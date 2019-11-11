import lndmanage.grpc_compiled.rpc_pb2 as ln

import logging
import time

from lndmanage.lib.user import yes_no_question
from lndmanage.lib.forwardings import ForwardingAnalyzer

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class FeeSetter(object):
    """
    Class for setting fees.
    """
    def __init__(self, node):
        """
            :param: node: `class`:lib.node.Node
        """
        self.node = node
        self.forwarding_analyzer = ForwardingAnalyzer(node)
        self.channel_fee_policies = node.get_channel_fee_policies()

    def set_fees_demand(self, cltv=20, base_fee_msat=40, from_days_ago=7,
                        min_fee_rate=0.000004, reckless=False):
        """
        Sets channel fee rates by estimating an economic demand factor.
        The change factor is based on four quantities, the unbalancedness,
        the fund flow, the fees collected (in a certain time frame) and
        the remaining remote balance.
        :param cltv: int
        :param base_fee_msat: int
        :param from_days_ago: int, forwarding history is taken over the past
                                   from_days_ago
        :param min_fee_rate: float, the fee rate will be not set lower than
                                    this amount
        :param reckless: bool, if set, there won't be any user interaction
        """
        time_end = time.time()
        time_start = time_end - from_days_ago * 24 * 60 * 60
        self.time_interval_days = from_days_ago
        self.forwarding_analyzer.initialize_forwarding_data(
            time_start, time_end)

        channels = self.node.get_all_channels()
        channels_forwarding_stats = \
            self.forwarding_analyzer.get_forwarding_statistics_channels()
        channel_fee_policies = self.fee_rate_change(
            channels, channels_forwarding_stats, base_fee_msat, cltv,
            min_fee_rate)

        if not reckless:
            logger.info("Do you want to set these fees? Enter [yes/no]:")
            if yes_no_question():
                self.node.set_channel_fee_policies(channel_fee_policies)
                logger.info("Have set new fee policy.")
            else:
                logger.info("Didn't set new fee policy.")
        else:
            self.node.set_channel_fee_policies(channel_fee_policies)
            logger.info("Have set new fee policy.")

    def fee_rate_change(self, channels, channels_forwarding_stats,
                        base_fee_msat, cltv, min_fee_rate=0.000001):
        """
        Calculates and reports the changes of the new fee policy.
        :param channels: dict with basic channel information
        :param channels_forwarding_stats: dict with forwarding information
        :param base_fee_msat: int
        :param cltv: int
        :param min_fee_rate: float, the fee rate will be not smaller than this
                                    parameter
        :return: dict, channel fee policies
        """
        logger.info("Determining new channel policies based on demand.")
        logger.info("Every channel will have a base fee of %d msat and cltv "
                    "of %d.", base_fee_msat, cltv)
        channel_fee_policies = {}

        for channel_id, channel_data in channels.items():
            channel_stats = channels_forwarding_stats.get(channel_id, None)
            if channel_stats is None:
                flow = 0
                fees_sat = 0
                total_forwarding_in = 0
                total_forwarding_out = 0
                total_forwarding = 0
                number_forwardings = 0
            else:
                flow = channel_stats['flow_direction']
                fees_sat = channel_stats['fees_total'] / 1000
                total_forwarding_in = channel_stats['total_forwarding_in']
                total_forwarding_out = channel_stats['total_forwarding_out']
                total_forwarding = total_forwarding_in + total_forwarding_out
                number_forwardings = channel_stats['number_forwardings']

            ub = channel_data['unbalancedness']
            capacity = channel_data['capacity']

            fee_rate = \
                self.channel_fee_policies[
                    channel_data['channel_point']]['fee_rate']

            logger.info(">>> New channel policy for channel %s", channel_id)
            logger.info(
                "    ub: %0.2f flow: %0.2f, fees: %1.3f sat, cap: %d sat, "
                "nfwd: %d, in: %d sat, out: %d sat.", ub, flow, fees_sat, capacity,
                 number_forwardings, total_forwarding_in, total_forwarding_out)

            # we want to give the demand the highest weight of the three
            # indicators
            # TODO: optimize those parameters
            wgt_demand = 1.2
            wgt_ub = 1.0
            wgt_flow = 0.6

            factor_demand = self.factor_demand(total_forwarding_out, capacity)
            factor_unbalancedness = self.factor_unbalancedness(ub)
            factor_flow = self.factor_flow(flow)
            # in the case where no forwarding was done, ignore the flow factor
            if total_forwarding == 0:
                wgt_flow = 0

            # calculate weighted change
            weighted_change = (
                wgt_ub * factor_unbalancedness +
                wgt_flow * factor_flow +
                wgt_demand * factor_demand
            ) / (wgt_ub + wgt_flow + wgt_demand)

            logger.info(
                "    Change factors: demand: %1.3f, "
                "unbalancedness %1.3f, flow: %1.3f. Weighted change: %1.3f",
                factor_demand, factor_unbalancedness, factor_flow,
                weighted_change)

            # for small fee rates we need to exaggerate the change in order
            # to get a change
            if fee_rate <= 2E-6:
                weighted_change = 1 + (weighted_change - 1) * 3

            fee_rate_new = fee_rate * weighted_change
            fee_rate_new = max(min_fee_rate, fee_rate_new)

            logger.info("    Fee rate: %1.6f -> %1.6f",
                        fee_rate, fee_rate_new)

            # give parsable output
            logger.debug(
                f"stats: {time.time():.0f} {channel_id} "
                f"{total_forwarding_in} {total_forwarding_out} "
                f"{ub:.3f} {flow:.3f} "
                f"{fees_sat:.3f} {capacity} {factor_demand:.3f} "
                f"{factor_unbalancedness:.3f} {factor_flow:.3f} "
                f"{weighted_change:.3f} {fee_rate:.6f} {fee_rate_new:.6f}")

            channel_fee_policies[channel_data['channel_point']] = {
                'base_fee_msat': base_fee_msat,
                'fee_rate': fee_rate_new,
                'cltv': cltv,
            }

        return channel_fee_policies

    @staticmethod
    def factor_unbalancedness(ub):
        """
        Calculates a change rate for the unbalancedness.
        The lower the unbalancedness, the lower the fee rate should be.
        This encourages outward flow through this channel.
        :param ub: float, in [-1 ... 1]
        :return: float, [1-c_max, 1+c_max]
        """
        # maximal change
        c_max = 0.50
        # give unbalancedness a more refined weight
        rescale = 0.5

        c = 1 + ub * rescale
        # limit the change
        if c > 1:
            return min(c, 1 + c_max)
        else:
            return max(c, 1 - c_max)

    @staticmethod
    def factor_flow(flow):
        """
        Calculates a change rate for the flow rate.
        If forwardings are predominantly flowing outward, we want to increase
        the fee rate, because there seems to be demand.
        :param flow: float, [-1 ... 1]
        :return: float, [1-c_max, 1+c_max]
        """
        c_max = 0.5
        rescale = 0.5
        c = 1 + flow * rescale

        # limit the change
        if c > 1:
            return min(c, 1 + c_max)
        else:
            return max(c, 1 - c_max)

    def factor_demand(self, amount_out, capacity):
        """
        Calculates a change factor by taking into account the amount transacted
        in a time interval compared to the channel's capcacity.
        The higher the amount forwarded, the larger the fee rate should be. The
        amount forwarded is estimated dividing the fees_sat with the current
        fee_rate.
        max_x is an empirical parameter that could be tuned in the future
        The model for the change rate is determined by a linear function:
        change = m * fee / fee_rate / capacity / time_interval_days + t
        :param amount_out: float
        :param capacity: int, capacity of channel
        :return: float, [1-c_max, 1+c_max]
        """
        logger.info("    Outward forwarded amount: %6.0f",
                    amount_out)
        rate = amount_out / self.time_interval_days

        c_min = 0.25  # change by 25% downwards
        c_max = 1.00  # change by 100% upwards
        # rate_target = 0.10 * capacity / 7  # target rate is 10% of capacity
        rate_target = 100000 / 7  # target rate is 200000 sat per week

        c = c_min * (rate / rate_target - 1) + 1

        return min(c, 1 + c_max)


if __name__ == '__main__':
    from lndmanage.lib.node import LndNode
    import logging.config
    from lndmanage import settings

    logging.config.dictConfig(settings.logger_config)

    nd = LndNode()
    fee_setter = FeeSetter(nd)
    fee_setter.set_fees_demand()
