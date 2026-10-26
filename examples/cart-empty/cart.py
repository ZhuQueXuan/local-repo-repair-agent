from functools import reduce
from operator import add


def cart_total(prices):
    return reduce(add, prices)
