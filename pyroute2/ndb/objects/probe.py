'''
Run a network probe
===================

A successful network probe neither creates real network objects
like interfaces or addresses, nor database records. The only
important thing it does -- it raises no exception.

On the contrary, an unsuccessful network probe raises a
`NetlinkError` exception, cancelling the whole transaction.

A network probe is always run from the corresponding netlink
target: a local system, a remote system, a network namespace,
a container.

An example scenario:

    * target alpha, set up eth0 10.0.0.2/24
    * target beta, set up eth0 10.0.0.4/24
    * ping 10.0.0.4 (beta) from target alpha
    * ping 10.0.0.2 (alpha) from target beta

The code below sets up the addresses and checks ICMP responses. If
any step fails, the whole transaction will be rolled back automatically::

    with NDB(log='debug') as ndb:
        ndb.sources.add(kind='remote', hostname='alpha', username='root')
        ndb.sources.add(kind='remote', hostname='beta', username='root')

        with ndb.begin() as trx:
            trx.push(
                (ndb
                    .interfaces[{'target': 'alpha', 'ifname': 'eth0'}]
                    .set(state='up')
                    .add_ip(address='10.0.0.2', prefixlen=24)
                ),
                (ndb
                    .interfaces[{'target': 'beta', 'ifname': 'eth0'}]
                    .set(state='up')
                    .add_ip(address='10.0.0.4', prefixlen=24)
                ),
                (ndb
                    .probes
                    .create(target='alpha', kind='ping', dst='10.0.0.4')
                ),
                (ndb
                    .probes
                    .create(target='beta', kind='ping', dst='10.0.0.2')
                ),
            )
'''

from pyroute2.netlink.rtnl.probe_msg import probe_msg

from ..objects import RTNL_Object

schema = probe_msg.sql_schema().unique_index(
    'family',
    'proto',
    'port',
    'dst_len',
    'PROBE_KIND',
    'PROBE_SRC',
    'PROBE_DST',
    'PROBE_HOSTNAME',
)

init = {
    'specs': [['probes', schema]],
    'classes': [['probes', probe_msg]],
    'event_map': {probe_msg: ['probes']},
}


class Probe(RTNL_Object):

    table = 'probes'
    msg_class = probe_msg
    api = 'probe'

    def __init__(self, *argv, **kwarg):
        kwarg['iclass'] = probe_msg
        self.event_map = {probe_msg: 'load_rtnlmsg'}
        super().__init__(*argv, **kwarg)

    def update(self):
        self['generation'] += 1
        return self
