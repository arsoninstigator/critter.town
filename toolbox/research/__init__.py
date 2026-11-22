"""research: the engines, called not copied.

three providers behind one shape, because a research loop that can only ask one engine is
a loop with one engine's opinion in it:

    search    tavily and querit, normalized to the same row
    fan_out   ask every configured engine and merge by url, keeping which engines agreed
    scrape    firecrawl, already a toolbox entry of its own
    collect   apify, for a dataset rather than a search
"""

from toolbox.research.apify import ApifyClient, make_apify
from toolbox.research.search import (QueritSearch, TavilySearch, fan_out, make_querit,
                                     make_tavily, normalize)

__all__ = ["ApifyClient", "make_apify", "QueritSearch", "TavilySearch", "fan_out",
           "make_querit", "make_tavily", "normalize"]
