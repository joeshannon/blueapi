from multiprocessing import Pool
from time import sleep
import os

from blueapi.core.context import BlueskyContext


MY = {"a": "p", "b": "q"}


def add(a, b):
    return a + b


def get_pid():
    return os.getpid()


def get_plans(ctx):
    return ctx.plans


if __name__ == "__main__":
    print("main")
    print(f"PID: {get_pid()}")

    ctx = BlueskyContext()

    with Pool(processes=1) as p:
        r = p.apply(add, [6, 2])
        r2 = p.apply(sleep, [2])
        r3 = p.apply(get_pid)
        r4 = p.apply(MY.__getitem__, ["a"])
        print(r)
        print(f"PID T: {r3}")
        print(r4)
        pass
