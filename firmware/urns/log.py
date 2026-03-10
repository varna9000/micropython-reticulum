# µReticulum Logging
# Lightweight, no threading, stdout only

import time

LOG_NONE     = -1
LOG_CRITICAL = 0
LOG_ERROR    = 1
LOG_WARNING  = 2
LOG_NOTICE   = 3
LOG_INFO     = 4
LOG_VERBOSE  = 5
LOG_DEBUG    = 6
LOG_EXTREME  = 7

loglevel = LOG_NOTICE

_level_names = {
    0: "CRIT", 1: "ERR ", 2: "WARN",
    3: "NOTE", 4: "INFO", 5: "VERB",
    6: "DBG ", 7: "XTRA",
}


def log(msg, level=LOG_NOTICE):
    if loglevel >= level:
        try:
            ln = _level_names.get(level, "????")
            print("[%d][%s] %s" % (time.time(), ln, str(msg)))
        except:
            pass


def set_loglevel(level):
    global loglevel
    loglevel = level


def trace_exception(e):
    import sys
    sys.print_exception(e)
