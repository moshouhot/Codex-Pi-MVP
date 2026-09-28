'use strict';
/*
 * pi-delegate exit forensics guard (CommonJS preload).
 *
 * Purpose: leave bounded, synchronous evidence about *how* a real Node worker
 * terminated, so a supervisor can conservatively classify an abnormal exit
 * (for example Windows 0xFFFFFFFF / signed -1) instead of guessing.
 *
 * Hard constraints (see TASK.md):
 *   - Observational only: never suppress, convert, or re-raise a failure.
 *   - Never install an `unhandledRejection` listener (that would change Node's
 *     default behavior). Only the passive `uncaughtExceptionMonitor` is used.
 *   - Never write to stdout/stderr and never throw into the host process.
 *   - Logging is synchronous, bounded and best-effort.
 *   - Do not record prompt text, credentials, full argv, environment values or
 *     tool arguments. Only the metadata listed in TASK.md is emitted.
 *
 * Activation is controlled by the environment variable
 * `PI_DELEGATE_EXIT_FORENSICS_PATH` (absolute path to a JSONL evidence file).
 * If it is missing the guard is a no-op.
 */

(function () {
  try {
    var logPath = process.env.PI_DELEGATE_EXIT_FORENSICS_PATH;
    if (!logPath || typeof logPath !== 'string') {
      return;
    }

    var invocationId = process.env.PI_DELEGATE_EXIT_FORENSICS_INVOCATION || null;

    var fs = require('fs');

    var GUARD_VERSION = 1;
    var MAX_STACK = 4000;
    var MAX_MESSAGE = 1000;
    var MAX_LINE = 16384;
    var MAX_FAILURES = 5;

    var failures = 0;

    function nowIso() {
      try {
        return new Date().toISOString();
      } catch (e) {
        return null;
      }
    }

    function clip(value, max) {
      try {
        if (value === undefined) {
          return undefined;
        }
        if (value === null) {
          return null;
        }
        var text = typeof value === 'string' ? value : String(value);
        if (text.length > max) {
          return text.slice(0, max) + '...[truncated]';
        }
        return text;
      } catch (e) {
        return undefined;
      }
    }

    function append(record) {
      if (failures >= MAX_FAILURES) {
        return;
      }
      try {
        record.source = 'guard';
        record.guard = 'pi-delegate-exit-forensics';
        record.guard_version = GUARD_VERSION;
        if (invocationId) {
          record.invocation = invocationId;
        }
        record.ts = nowIso();
        record.pid = process.pid;
        var line = JSON.stringify(record);
        if (typeof line !== 'string') {
          return;
        }
        if (line.length > MAX_LINE) {
          line = line.slice(0, MAX_LINE);
        }
        fs.appendFileSync(logPath, line + '\n');
      } catch (e) {
        // Bounded best-effort: stop trying after repeated failures.
        failures += 1;
      }
    }

    function describeCode(code) {
      var out = { code_type: typeof code };
      try {
        if (code === undefined) {
          out.code = null;
          out.code_undefined = true;
          return out;
        }
        if (typeof code === 'number') {
          out.code = code;
          if (isFinite(code)) {
            var unsigned = code >>> 0;
            out.code_unsigned_32 = unsigned;
            out.code_signed_32 = unsigned >= 0x80000000 ? unsigned - 0x100000000 : unsigned;
            var hex = unsigned.toString(16).toUpperCase();
            while (hex.length < 8) {
              hex = '0' + hex;
            }
            out.code_hex = '0x' + hex;
          }
          return out;
        }
        if (code === null) {
          out.code = null;
          return out;
        }
        out.code = clip(String(code), 64);
        return out;
      } catch (e) {
        return out;
      }
    }

    function exitCodeProperty() {
      try {
        var value = process.exitCode;
        if (typeof value === 'number' || typeof value === 'string') {
          return value;
        }
        return null;
      } catch (e) {
        return null;
      }
    }

    function describeError(err, origin) {
      var out = { origin: clip(origin, 128) };
      try {
        if (err && typeof err === 'object') {
          out.error_name = clip(err.name, 128);
          out.error_message = clip(err.message, MAX_MESSAGE);
          if (typeof err.stack === 'string') {
            out.error_stack = clip(err.stack, MAX_STACK);
          }
          if (err.code !== undefined) {
            out.error_code = clip(err.code, 64);
          }
        } else {
          out.error_message = clip(err, MAX_MESSAGE);
        }
      } catch (e) {
        // ignore
      }
      return out;
    }

    // --- startup evidence -------------------------------------------------
    append({
      event: 'guard_start',
      node_version: clip(process.version, 64),
      platform: clip(process.platform, 32),
      arch: clip(process.arch, 32),
      cwd: clip(process.cwd(), 512),
      ppid: typeof process.ppid === 'number' ? process.ppid : null,
      exit_code_at_start: exitCodeProperty()
    });

    // --- explicit JS exit request ----------------------------------------
    try {
      var originalExit = process.exit;
      if (typeof originalExit === 'function') {
        process.exit = function (code) {
          var record = describeCode(code);
          record.event = 'js_process_exit';
          record.exit_code_property = exitCodeProperty();
          append(record);
          return originalExit.apply(process, arguments);
        };
      }
    } catch (e) {
      // ignore
    }

    try {
      var originalReallyExit = process.reallyExit;
      if (typeof originalReallyExit === 'function') {
        process.reallyExit = function (code) {
          var record = describeCode(code);
          record.event = 'js_really_exit';
          append(record);
          return originalReallyExit.apply(process, arguments);
        };
      }
    } catch (e) {
      // ignore
    }

    // --- passive lifecycle observation ------------------------------------
    try {
      process.on('beforeExit', function (code) {
        append({
          event: 'before_exit',
          code: typeof code === 'number' ? code : null,
          exit_code_property: exitCodeProperty()
        });
      });
    } catch (e) {
      // ignore
    }

    try {
      process.on('exit', function (code) {
        var record = describeCode(code);
        record.event = 'exit_event';
        record.exit_code_property = exitCodeProperty();
        append(record);
      });
    } catch (e) {
      // ignore
    }

    // Passive monitor: unlike `uncaughtException` it never changes the default
    // crash behavior of the process.
    try {
      process.on('uncaughtExceptionMonitor', function (err, origin) {
        var record = describeError(err, origin);
        record.event = 'uncaught_exception';
        append(record);
      });
    } catch (e) {
      // ignore
    }
  } catch (e) {
    // Never throw into the host process.
  }
})();
