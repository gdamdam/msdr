"""Env for subprocess.run(["node", ...]) on a machine whose node is missing
libsimdjson.30.dylib (seen on one Homebrew install): points DYLD_LIBRARY_PATH at whichever
installed simdjson keg actually has that file. Not itself a test module
(no test_ prefix) -- a plain helper imported by the tests that shell out to
node.

Setting DYLD_LIBRARY_PATH via `export` in a shell step ahead of pytest does
not reach node here: the dynamic linker strips DYLD_* from the host's
python3 before main() ever runs (confirmed: os.environ.get("DYLD_LIBRARY_PATH")
is already None inside python3 even right after `export`ing it in the same
shell invocation), so a value can only reach a `node` subprocess by building
the env dict in Python and passing it straight to subprocess.run(env=...),
never by reading it back out of this process's own (already-stripped)
os.environ.
"""
import glob
import os


def node_env():
    env = dict(os.environ)
    for path in sorted(glob.glob("/opt/homebrew/Cellar/simdjson/*/lib")):
        if os.path.exists(os.path.join(path, "libsimdjson.30.dylib")):
            env["DYLD_LIBRARY_PATH"] = path
            break
    return env
