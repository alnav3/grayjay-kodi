// Driver script run inside the standalone `qjs` (quickjs-ng) subprocess -
// the "qjs_subprocess" JSEngine backend (see jsengine.py). Replaces the
// embedded quickjs Python binding, whose bytecode compiler occasionally
// fails ("InternalError: unconsistent stack size") on YouTube's live,
// rotating player bundle. This engine build has that class of compiler bug
// fixed upstream and runs the real bundle cleanly.
//
// Protocol:
//   Outer channel (stdin/stdout, event-driven via os.setReadHandler so the
//   engine's job queue auto-drains between dispatches - required for native
//   Promise chains, e.g. YouTube's BotGuard flow, to resolve at all): Python
//   sends one JSON-encoded JS source string per line; this script indirect-
//   eval()s it (global scope - required, since eval() is called from a
//   module and indirect eval always runs in global scope regardless) and
//   writes back one JSON line {"ok":true,"result":<json>} or
//   {"ok":false,"error":"<message>"}.
//
//   Inner channel (two extra fds, numbers given as argv[1]/argv[2]): while
//   evaluating, host_prelude.js's __host_* globals (defined below onto
//   globalThis - required for indirect eval to see them) route synchronously
//   through this pair: JS writes a line and blocks reading the response, so a
//   nested host call can happen mid-eval without disturbing the outer
//   request/response framing on stdin/stdout.
import * as os from "os";
import * as std from "std";

var reqFd = parseInt(scriptArgs[1]);
var respFd = parseInt(scriptArgs[2]);
var reqFile = std.fdopen(reqFd, "w");
var respFile = std.fdopen(respFd, "r");

globalThis.__bridgeHostCall = function (name, payloadJson) {
  reqFile.puts(JSON.stringify({ call: name, payload: payloadJson }) + "\n");
  reqFile.flush();
  var line = respFile.getline();
  if (line === null) throw new Error("host bridge closed (fd " + respFd + ")");
  var msg = JSON.parse(line);
  if (msg.error !== undefined && msg.error !== null) throw new Error(msg.error);
  return msg.result === undefined ? null : msg.result;
};

// host_prelude.js calls global[name](jsonString) and expects a JSON string
// (or null) back - the same convention bridge.py's Python-bound callables
// use for the old quickjs backend, so host_prelude.js / dom.js / the plugin
// script itself need zero changes to run under this engine.
[
  "__host_log", "__host_http", "__host_http_batch", "__host_b64encode",
  "__host_b64decode", "__host_uuid", "__host_md5", "__host_dom_parse",
  "__host_dom_op", "__host_toast", "__host_sleep",
].forEach(function (name) {
  globalThis[name] = function (payloadJson) {
    return globalThis.__bridgeHostCall(name, payloadJson);
  };
});

os.setReadHandler(std.in.fileno(), function () {
  var line = std.in.getline();
  if (line === null) {
    os.setReadHandler(std.in.fileno(), null);
    std.exit(0);
    return;
  }
  var out;
  try {
    var code = JSON.parse(line);
    var result = (0, eval)(code); // indirect eval - always global scope
    out = { ok: true, result: result === undefined ? null : result };
  } catch (e) {
    out = { ok: false, error: (e && e.message) ? e.message : String(e) };
  }
  std.out.puts(JSON.stringify(out) + "\n");
  std.out.flush();
});
