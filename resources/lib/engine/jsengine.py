# -*- coding: utf-8 -*-
"""Pluggable JavaScript engine abstraction.

Grayjay source plugins are JavaScript executed against an embedded engine
(Grayjay itself uses V8). Inside Kodi we only have CPython, so we need a JS
runtime reachable from Python that also lets JS call *back* into Python
(HTTP, logging, crypto). Backends, in preference order:

1. ``qjs_subprocess`` - a standalone, statically-linked quickjs-ng CLI binary
                      (vendored per-arch), driven as a persistent subprocess
                      over stdin/stdout + a pair of pipes for host callbacks
                      (see qjs_driver.js). Newer and more spec-compliant than
                      the embedded quickjs Python binding below - in
                      particular it doesn't hit the bytecode-compiler bug
                      ("InternalError: unconsistent stack size") that binding
                      occasionally hits on YouTube's live, rotating player
                      bundle. Preferred whenever a matching binary is vendored
                      for the current machine.
2. ``quickjs``      - QuickJS Python bindings (in-process, native extension).
                      Small, supports host callables. Used when no
                      qjs_subprocess binary is vendored for this arch (e.g.
                      dev machines) but the extension is installed.
3. ``py_mini_racer`` - V8 bindings. Fast and battle-tested but eval-only
                      (no synchronous host callbacks), so HTTP has to be
                      pre-resolved. Used only as a fallback.
4. ``js2py``        - Pure Python (ES5/6). Slow and partial, but needs no
                      compiler and can be VENDORED into the addon, so it runs
                      on locked-down targets (e.g. CoreELEC: no pip/compiler).
                      Supports host callables. Fallback when nothing above is
                      available.

The native backends need a build matching the target arch (and, for the
in-process quickjs binding, Kodi's bundled Python ABI too); js2py sidesteps
that entirely. See README.md.
"""
import os
import platform
import sys
import sysconfig

# Allow a vendored copy of pure-Python deps (js2py + pyjsparser) to be shipped
# inside the addon so no installation is needed on the target.
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


def _native_vendor_dir():
    """Locate a vendored native engine build matching this interpreter.

    Native extensions (e.g. quickjs) are arch + Python-version + ABI specific.
    The CoreELEC target is 32-bit ARM (armv7l) on CPython 3.11, even though the
    kernel reports aarch64 — so we key on the actual userspace machine and the
    Python tag, not on uname. Falls back to the pure-Python js2py backend when
    no matching build is present (e.g. on a dev machine).
    """
    machine = platform.machine()  # e.g. 'armv7l'
    pyver = "cp%d%d" % sys.version_info[:2]
    candidates = [
        "%s-%s" % (machine, pyver),                      # armv7l-cp311
        "%s-%s" % (sysconfig.get_platform(), pyver),     # linux-armv7l-cp311 variants
    ]
    root = os.path.join(_HERE, "vendor_native")
    for c in candidates:
        d = os.path.join(root, c)
        if os.path.isdir(d):
            return d
    return None


_NATIVE = _native_vendor_dir()
if _NATIVE and _NATIVE not in sys.path:
    sys.path.insert(0, _NATIVE)


def _qjs_subprocess_binary():
    """Locate a vendored standalone qjs binary matching this machine's arch.

    Unlike the in-process quickjs Python extension, this is a plain executable
    (statically linked) - only the CPU arch needs to match, not the Python
    ABI/version."""
    machine = platform.machine()  # e.g. 'armv7l'
    path = os.path.join(_HERE, "vendor_native", machine, "bin", "qjs")
    return path if os.path.isfile(path) else None


_QJS_SUBPROCESS_BIN = _qjs_subprocess_binary()
_QJS_SUBPROCESS_DRIVER = os.path.join(_HERE, "qjs_driver.js")


def _preload_libatomic():
    """The armv7l quickjs build references 64-bit atomic intrinsics that 32-bit
    ARM provides via libatomic. CPython doesn't link it, so the extension fails
    with `undefined symbol: __atomic_*`. Load it RTLD_GLOBAL first so those
    symbols resolve. No-op (and harmless) where libatomic is absent/unneeded."""
    if not _NATIVE:
        return
    try:
        import ctypes
        ctypes.CDLL("libatomic.so.1", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass


_preload_libatomic()

from ..kodiutils import log


import re

# Matches a run of backslashes followed by '-'. If the run length is odd, the
# trailing backslash escapes the hyphen (\-) and is rewritten to \x2d.
_DASH_ESCAPE_RE = re.compile(r"(\\+)-")


def _dash_repl(m):
    bs = m.group(1)
    if len(bs) % 2 == 1:
        return bs[:-1] + r"\x2d"
    return m.group(0)


class JSError(Exception):
    pass


class JSEngine(object):
    """Common interface over whichever backend is available."""

    def __init__(self):
        self.backend = None
        self._ctx = None
        self._load_backend()

    def _load_backend(self):
        forced = os.environ.get("GRAYJAY_JS_BACKEND")
        if forced:
            init = {
                "qjs_subprocess": self._init_qjs_subprocess,
                "quickjs": self._init_quickjs,
                "py_mini_racer": self._init_mini_racer,
                "js2py": self._init_js2py,
            }.get(forced)
            if init is None:
                raise JSError("Unknown GRAYJAY_JS_BACKEND=%s" % forced)
            self.backend = forced
            init()
            log("JS backend: %s (forced)" % forced, "info")
            return
        if _QJS_SUBPROCESS_BIN:
            try:
                self._init_qjs_subprocess()
                self.backend = "qjs_subprocess"
                log("JS backend: qjs_subprocess (%s)" % _QJS_SUBPROCESS_BIN, "info")
                return
            except Exception as exc:
                log("qjs_subprocess unavailable, falling back: %s" % exc, "warning")
        try:
            import quickjs  # noqa: F401
            self.backend = "quickjs"
            self._init_quickjs()
            log("JS backend: quickjs", "info")
            return
        except ImportError:
            pass
        try:
            import py_mini_racer  # noqa: F401
            self.backend = "py_mini_racer"
            self._init_mini_racer()
            log("JS backend: py_mini_racer (host callbacks unavailable)", "warning")
            return
        except ImportError:
            pass
        try:
            import js2py  # noqa: F401
            self.backend = "js2py"
            self._init_js2py()
            log("JS backend: js2py (pure Python, slow)", "info")
            return
        except ImportError:
            pass
        raise JSError(
            "No JavaScript engine available. Install 'quickjs' (preferred), "
            "'py_mini_racer', or vendor 'js2py' into resources/lib/engine/vendor. "
            "See README.md."
        )

    # -- qjs_subprocess -----------------------------------------------------
    def _init_qjs_subprocess(self):
        import subprocess
        req_r, req_w = os.pipe()   # child writes host-call requests to req_w
        resp_r, resp_w = os.pipe()  # parent writes host-call responses to resp_w
        try:
            proc = subprocess.Popen(
                [_QJS_SUBPROCESS_BIN, "-m", _QJS_SUBPROCESS_DRIVER,
                 str(req_w), str(resp_r)],
                pass_fds=(req_w, resp_r),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=1,
                universal_newlines=True,
            )
        finally:
            os.close(req_w)
            os.close(resp_r)
        self._ctx = proc
        self._qjssub_req_r = req_r    # parent reads host-call requests here
        self._qjssub_resp_w = resp_w  # parent writes host-call responses here
        self._qjssub_fns = {}

    def _qjssub_dispatch_hostcall(self):
        import json
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = os.read(self._qjssub_req_r, 65536)
            if not chunk:
                raise JSError("qjs_subprocess host-call channel closed unexpectedly")
            buf += chunk
        msg = json.loads(buf.decode("utf-8"))
        name = msg.get("call")
        fn = self._qjssub_fns.get(name)
        try:
            if fn is None:
                raise JSError("no host function registered for %s" % name)
            resp = {"result": fn(msg.get("payload"))}
        except Exception as exc:
            resp = {"error": str(exc)}
        os.write(self._qjssub_resp_w, (json.dumps(resp) + "\n").encode("utf-8"))

    def _qjssub_eval(self, code):
        import json
        import select
        p = self._ctx
        if p.poll() is not None:
            err = p.stderr.read() if p.stderr else ""
            raise JSError("qjs_subprocess exited (code %s): %s" % (p.returncode, err))
        p.stdin.write(json.dumps(code) + "\n")
        p.stdin.flush()
        while True:
            r, _, _ = select.select([p.stdout, self._qjssub_req_r], [], [], 90.0)
            if not r:
                raise JSError("qjs_subprocess timed out waiting for a response")
            if self._qjssub_req_r in r:
                self._qjssub_dispatch_hostcall()
                continue
            line = p.stdout.readline()
            if not line:
                err = p.stderr.read() if p.stderr else ""
                raise JSError("qjs_subprocess closed stdout unexpectedly: %s" % err)
            msg = json.loads(line)
            if not msg.get("ok"):
                raise JSError(msg.get("error") or "unknown qjs_subprocess error")
            return msg.get("result")

    def _qjssub_register(self, name, fn):
        self._qjssub_fns[name] = fn

    def _qjssub_call(self, fn_name, *json_args):
        import json
        arg_list = ",".join(json_args)
        code = "JSON.stringify((%s)(%s))" % (fn_name, arg_list)
        out = self._qjssub_eval(code)
        return json.loads(out) if out is not None else None

    def _qjssub_run_async(self, deadline_s, max_iter):
        """Mirrors _qjs_run_async: the engine's own event loop auto-drains the
        native promise/job queue between dispatches (validated: a .then()
        scheduled in one eval() call is visible by the next), so no separate
        drain step is needed here - just poll host_prelude.js's JS-level timer
        queue and async slot exactly as the in-process quickjs backend does."""
        import json
        import time
        start = time.time()
        it = 0
        while True:
            if (time.time() - start) > deadline_s or it > max_iter:
                raise JSError("async call timed out after %.1fs (%d iters)"
                              % (time.time() - start, it))
            res = json.loads(self._qjssub_eval("__bridge_async_result()"))
            if res.get("__done"):
                return res.get("result")
            if "__error" in res:
                raise JSError(res["__error"])
            ran_timer = bool(self._qjssub_eval("__run_one_timer()"))
            it += 1
            if not ran_timer:
                raise JSError("async call stalled: no pending timers "
                               "but result never settled")

    # -- quickjs ----------------------------------------------------------
    def _init_quickjs(self):
        import quickjs
        self._ctx = quickjs.Context()
        # The YouTube BotGuard VM is deeply recursive; the default quickjs stack
        # (~256KB) overflows running it. Give it room. Harmless elsewhere.
        for setter, val in (("set_max_stack_size", 4 * 1024 * 1024),):
            fn = getattr(self._ctx, setter, None)
            if fn:
                try:
                    fn(val)
                except Exception:
                    pass

    def _qjs_eval(self, code):
        try:
            return self._ctx.eval(code)
        except Exception as exc:  # quickjs raises its own error types
            raise JSError(str(exc))

    def _qjs_register(self, name, fn):
        # quickjs marshals args/returns as JSON-compatible primitives.
        self._ctx.add_callable(name, fn)

    def _qjs_drain_jobs(self):
        """Run the pending promise/microtask jobs; return how many ran."""
        n = 0
        while True:
            try:
                ran = self._ctx.execute_pending_job()
            except Exception as exc:
                raise JSError(str(exc))
            if not ran:
                return n
            n += 1

    def _qjs_run_async(self, deadline_s, max_iter):
        """Drive the JS event loop (host_prelude timer queue + promise jobs)
        until the pending async bridge call settles. Returns the decoded result
        or raises JSError on rejection / timeout / stall."""
        import json
        import time
        start = time.time()
        it = 0
        while True:
            if (time.time() - start) > deadline_s or it > max_iter:
                raise JSError("async call timed out after %.1fs (%d iters)"
                              % (time.time() - start, it))
            ran_jobs = self._qjs_drain_jobs()
            res = json.loads(self._ctx.eval("__bridge_async_result()"))
            if res.get("__done"):
                return res.get("result")
            if "__error" in res:
                raise JSError(res["__error"])
            ran_timer = bool(self._ctx.eval("__run_one_timer()"))
            it += 1
            if not ran_timer and ran_jobs == 0:
                raise JSError("async call stalled: no pending jobs or timers "
                              "but result never settled")

    def _qjs_call(self, fn_name, *json_args):
        # Call a JS function by name with already-JSON-encoded string args.
        import json
        arg_list = ",".join(json_args)
        code = "JSON.stringify((%s)(%s))" % (fn_name, arg_list)
        out = self._qjs_eval(code)
        return json.loads(out) if out is not None else None

    # -- py_mini_racer ----------------------------------------------------
    def _init_mini_racer(self):
        import py_mini_racer
        self._ctx = py_mini_racer.MiniRacer()

    def _mr_eval(self, code):
        try:
            return self._ctx.eval(code)
        except Exception as exc:
            raise JSError(str(exc))

    def _mr_register(self, name, fn):
        raise JSError(
            "py_mini_racer cannot register host callables; HTTP-driven "
            "plugins require the quickjs backend."
        )

    def _mr_call(self, fn_name, *json_args):
        import json
        arg_list = ",".join(json_args)
        code = "JSON.stringify((%s)(%s))" % (fn_name, arg_list)
        out = self._mr_eval(code)
        return json.loads(out) if out is not None else None

    # -- js2py ------------------------------------------------------------
    def _init_js2py(self):
        import js2py
        self._ctx = js2py.EvalJs(enable_require=False)

    def _j2p_eval(self, code):
        try:
            res = self._ctx.eval(code)
        except Exception as exc:
            raise JSError(str(exc))
        # js2py returns its own JsObjectWrapper / primitives; coerce to str
        # when the caller asked for a JSON.stringify result.
        if res is None:
            return None
        return str(res)

    def _j2p_register(self, name, fn):
        # Assigning a Python callable onto the context exposes it to JS.
        setattr(self._ctx, name, fn)

    def _j2p_call(self, fn_name, *json_args):
        import json
        arg_list = ",".join(json_args)
        out = self._j2p_eval("JSON.stringify((%s)(%s))" % (fn_name, arg_list))
        return json.loads(out) if out else None

    # -- public api -------------------------------------------------------
    def eval(self, code):
        if self.backend == "qjs_subprocess":
            return self._qjssub_eval(code)
        if self.backend == "quickjs":
            return self._qjs_eval(code)
        if self.backend == "js2py":
            return self._j2p_eval(code)
        return self._mr_eval(code)

    def register(self, name, fn):
        """Expose a Python callable to JS under a global name."""
        if self.backend == "qjs_subprocess":
            return self._qjssub_register(name, fn)
        if self.backend == "quickjs":
            return self._qjs_register(name, fn)
        if self.backend == "js2py":
            return self._j2p_register(name, fn)
        return self._mr_register(name, fn)

    def call(self, fn_name, *json_args):
        """Call a global JS function; args are pre-serialized JSON strings."""
        if self.backend == "qjs_subprocess":
            return self._qjssub_call(fn_name, *json_args)
        if self.backend == "quickjs":
            return self._qjs_call(fn_name, *json_args)
        if self.backend == "js2py":
            return self._j2p_call(fn_name, *json_args)
        return self._mr_call(fn_name, *json_args)

    def run_async(self, deadline_s=60.0, max_iter=500000):
        """Pump the event loop until a pending async bridge call settles.

        Only the qjs_subprocess/quickjs backends have a real event loop; the
        others run plugins synchronously, so an async result there is
        unsupported.
        """
        if self.backend == "qjs_subprocess":
            return self._qjssub_run_async(deadline_s, max_iter)
        if self.backend == "quickjs":
            return self._qjs_run_async(deadline_s, max_iter)
        raise JSError("async source methods require the qjs_subprocess/quickjs backend")

    def eval_async(self, js_expr, deadline_s=60.0):
        """Evaluate a JS expression that's expected to return a Promise; pump
        the event loop until it settles, then return the resolved value.

        Used outside of `source.<method>` when the host has kicked off its
        own async work (e.g. materialising pending subtitle promises via
        host_prelude.js's `__await_pending_subs`). Wraps the expression in
        the same `__async_slot` protocol `__bridge_call` uses for source
        methods, so the existing event-loop pump picks it up."""
        import json
        wrapper = (
            "(function(){var p=(" + js_expr + ");"
            "if(p&&typeof p.then==='function'){"
            "var slot={done:false,value:undefined,error:undefined};"
            "p.then(function(v){slot.value=v;slot.done=true;},"
            "function(e){slot.error=(e&&e.message)?e.message:String(e);slot.done=true;});"
            "globalThis.__async_slot=slot;"
            "return JSON.stringify({__async:true});}"
            "return JSON.stringify(JSON.stringify(p));})()"
        )
        out = self.eval(wrapper)
        data = json.loads(out) if out else None
        if isinstance(data, dict) and data.get("__async"):
            return self.run_async(deadline_s=deadline_s)
        return data

    def drain_jobs(self):
        """Drain any pending promise/microtask jobs.

        quickjs (in-process) needs an explicit pump. qjs_subprocess drains its
        job queue to exhaustion as part of every dispatch already (validated:
        a .then() scheduled in one eval() is settled by the next), so there's
        nothing to do here for it. Other backends: no-op."""
        if self.backend == "quickjs":
            return self._qjs_drain_jobs()
        return 0

    def prepare(self, code):
        """Apply backend-specific source fixups before eval.

        quickjs 1.19.4 (the in-process Python binding) rejects an escaped
        hyphen (\\-) inside a /u (unicode) character class, which V8 accepts —
        so real plugins (e.g. YouTube's bundled JSDOM) fail to parse. Rewrite
        genuine \\- escapes to \\x2d (the same literal hyphen in both strings
        and regexes), leaving escaped backslashes (\\\\-) intact. Semantically
        neutral, so it's applied for qjs_subprocess too even though that
        engine doesn't appear to need it — cheap insurance against the same
        class of strictness in a future build.
        """
        if self.backend not in ("quickjs", "qjs_subprocess"):
            return code
        return _DASH_ESCAPE_RE.sub(_dash_repl, code)
