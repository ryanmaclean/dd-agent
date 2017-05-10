"""
Unbound DnS check
"""

from __future__ import division

# stdlib
import subprocess

# project
from checks import AgentCheck

UNBOUND_NAMESPACE = 'unbound.'


class Unbound(AgentCheck):
    # Inject dependency so that we can make mocks work in UnitTests
    subprocess = subprocess

    def check(self, instance):
        stats = self._collect_statistics()

        for line in stats:
            k,value = line.split('=')
            # Don't send individual thread stats: use totals only
            if 'total.' in k:
                metric = UNBOUND_NAMESPACE + k
                # If the metric contains 'num', it's a monotonic increment and should be sent as a histogram to DD
                if '.num.' in k:
                    self.log.debug("HISTOGRAM: " + metric + " : " + value)
                    self.histogram(metric=metric, value=value)
                else:
                    self.log.debug("GAUGE: " + metric + " : " + value)
                    self.gauge(metric=metric, value=value)

    def _collect_statistics(self):
        """
        Get stats from unbound
        :return: stats
        """
        p = self.subprocess.Popen(
            'sudo unbound-control stats_noreset'.split(),
            stdout=self.subprocess.PIPE
        )
        stats, err = p.communicate()
        return filter(None, stats.split('\n'))