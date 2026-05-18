import sys


def safe_print(*args, sep=" ", end="\n"):
    message = sep.join(str(arg) for arg in args) + end
    try:
        print(*args, sep=sep, end=end)
    except UnicodeEncodeError:
        stdout = sys.stdout
        buffer = getattr(stdout, "buffer", None)
        if buffer is not None:
            encoding = getattr(stdout, "encoding", None) or "utf-8"
            buffer.write(message.encode(encoding, errors="backslashreplace"))
            buffer.flush()
            return
        stdout.write(message.encode("ascii", errors="backslashreplace").decode("ascii"))
