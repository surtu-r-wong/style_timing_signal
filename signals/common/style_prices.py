"""Read genuine standard/pure small-cap series in one aligned PG snapshot."""
from signals.common import data_source

SMALL_NAMES = ("中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值")
PURE_NAMES = {name: name[:-2] + "纯" + name[-2:] for name in SMALL_NAMES}


def price_panels(names, start=None, end=None):
    pure_names = [PURE_NAMES.get(name, name) for name in names]
    required = list(dict.fromkeys([*names, *pure_names]))
    frame = data_source.load_pg_closes(required, start=start, end=end,
                                       trim_ragged_tail=True, trim_ragged_head=True)
    return frame[names], frame[pure_names].set_axis(names, axis=1)
