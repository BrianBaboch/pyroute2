'''Compatibility with older but supported Python versions'''

try:
    from enum import StrEnum
except ImportError:
    # StrEnum appeared in python 3.11

    from enum import Enum

    class StrEnum(str, Enum):
        '''Same as enum, but members are also strings.'''


try:
    from socket import ETHERTYPE_IP
except ImportError:
    # ETHERTYPE_* are new in python 3.12
    ETHERTYPE_IP = 0x800


__all__ = ('StrEnum', 'ETHERTYPE_IP')