"""Keep macOS from throttling KiCad while a long job runs.

macOS App Nap slows an app whose windows are hidden or behind others to a
fraction of its speed: a routing job left to run while the user reads mail
took three to five times as long (search, board reads and the progress
display alike) and ran into its time limit with connections still open.
A job holds an NSProcessInfo activity, the system's way of saying the
user started this and is waiting for it; nothing changes on other systems.
The Objective-C runtime is called through ctypes, since KiCad's Python has
no PyObjC."""
import contextlib
import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)

# NSActivityOptions: user initiated (no App Nap, no idle system sleep) and
# latency critical (no timer coalescing)
_USER_INITIATED = 0x00FFFFFF
_LATENCY_CRITICAL = 0xFF00000000

_objc = None


def _runtime():
    global _objc
    if _objc is None:
        path = ctypes.util.find_library("objc")
        if not path:
            raise OSError("no Objective-C runtime")
        _objc = ctypes.cdll.LoadLibrary(path)
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return _objc


def _send(obj, selector: str, *args, argtypes=()):
    objc = _runtime()
    # a fresh function object per signature: objc_msgSend is called with different ones
    fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
    return fn(obj, objc.sel_registerName(selector.encode()), *args)


def begin(reason: str):
    """Start an activity; returns a token for end(), or None where there is
    nothing to do or it is not available."""
    if sys.platform != "darwin":
        return None
    try:
        objc = _runtime()
        text = _send(objc.objc_getClass(b"NSString"), "stringWithUTF8String:", reason.encode(),
                     argtypes=(ctypes.c_char_p,))
        info = _send(objc.objc_getClass(b"NSProcessInfo"), "processInfo")
        token = _send(info, "beginActivityWithOptions:reason:", _USER_INITIATED | _LATENCY_CRITICAL, text,
                      argtypes=(ctypes.c_uint64, ctypes.c_void_p))
        if token:
            _send(token, "retain")
        return token or None
    except Exception as e:                # never let this stop a job
        logger.info("Could not hold off App Nap: %s", e)
        return None


def end(token) -> None:
    if not token:
        return
    try:
        info = _send(_runtime().objc_getClass(b"NSProcessInfo"), "processInfo")
        _send(info, "endActivity:", token, argtypes=(ctypes.c_void_p,))
        _send(token, "release")
    except Exception as e:
        logger.info("Could not end the activity: %s", e)


@contextlib.contextmanager
def awake(reason: str = "Autorouter is routing"):
    """with awake(): ... runs the block without App Nap."""
    token = begin(reason)
    try:
        yield token
    finally:
        end(token)
