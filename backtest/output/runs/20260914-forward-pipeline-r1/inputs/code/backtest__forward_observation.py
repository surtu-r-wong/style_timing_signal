"""Append-only local receipts for prospective signals; no orders or return tests.

CLI uses the actual clock. A receipt establishes what this machine read that
same evening, not vendor PIT accuracy or an externally notarized timestamp.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import date, datetime, timezone
import fcntl
import hashlib
import io
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
NAMES = {'equal_weight', 'slope20'}


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _clock(now):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone-aware clock required')
    return now.astimezone(timezone.utc)


def _repo_file(repo, relative):
    p = Path(relative)
    root = Path(repo).resolve()
    full = (root/p).resolve()
    if p.is_absolute() or not full.is_relative_to(root) or not full.is_file():
        raise ValueError('source must be an existing repository file')
    return full


@contextmanager
def _lock(campaign):
    with (Path(campaign)/'.lock').open('a') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield


def init_campaign(campaign, protocol_file, *, repo=ROOT, now=None):
    now = _clock(now)
    raw = Path(protocol_file).read_bytes()
    p = json.loads(raw)
    if p.get('timezone') != 'Asia/Shanghai' or set(p.get('sources', {})) != NAMES:
        raise ValueError('fixed timezone and both incumbent sources required')
    if p.get('earliest_record_hour') != 16 or p.get('fill') != 'next_actual_trading_day_close':
        raise ValueError('unsupported recording or fill convention')
    if date.fromisoformat(p['first_signal_date']) <= now.astimezone(ZoneInfo(p['timezone'])).date():
        raise ValueError('first signal day must be after campaign creation day')
    if not p.get('code_files'):
        raise ValueError('code guards required')
    for relative in p['sources'].values():
        _repo_file(repo, relative)
    guards = {relative:_sha(_repo_file(repo, relative).read_bytes()) for relative in p['code_files']}
    campaign = Path(campaign)
    campaign.mkdir(parents=True, exist_ok=False)
    (campaign/'records').mkdir()
    (campaign/'protocol.json').write_bytes(raw)
    meta = {'schema_version':1, 'created_at_utc':now.isoformat(),
            'protocol_sha256':_sha(raw), 'code_sha256':guards,
            'mode':'observation_only_no_statistical_go'}
    blob = _bytes(meta)
    (campaign/'campaign.json').write_bytes(blob)
    (campaign/'campaign.sha256').write_text(_sha(blob)+'\n')
    (campaign/'journal.jsonl').write_text('')
    return campaign


def _protocol(campaign):
    raw = (campaign/'campaign.json').read_bytes()
    if _sha(raw) != (campaign/'campaign.sha256').read_text().strip():
        raise ValueError('campaign metadata hash mismatch')
    meta = json.loads(raw)
    raw = (campaign/'protocol.json').read_bytes()
    if _sha(raw) != meta['protocol_sha256']:
        raise ValueError('protocol hash mismatch')
    return meta, json.loads(raw)


def _signal_rows(raw, decision_date):
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    if not rows or not {'date', 'factor_value'} <= set(rows[0]):
        raise ValueError('signal CSV requires date and factor_value')
    values = {}
    previous = None
    for row in rows:
        d = date.fromisoformat(row['date'])
        v = float(row['factor_value'])
        if previous is not None and d <= previous:
            raise ValueError('signal dates must be strictly increasing and unique')
        if not math.isfinite(v) or abs(v) > 1:
            raise ValueError('finite signal in [-1,1] required')
        values[d.isoformat()] = v
        previous = d
    if previous != decision_date:
        raise ValueError('signal must end on recording day: no stale or future rows')
    return values


def _verify(campaign):
    meta, protocol = _protocol(campaign)
    journal = [json.loads(line) for line in (campaign/'journal.jsonl').read_text().splitlines()]
    previous_hash = None
    previous_day = None
    previous_time = datetime.fromisoformat(meta['created_at_utc'])
    for entry in journal:
        d = date.fromisoformat(entry['decision_date'])
        if previous_day is not None and d <= previous_day:
            raise ValueError('journal dates not strictly increasing')
        folder = campaign/'records'/d.isoformat()
        blob = (folder/'record.json').read_bytes()
        if _sha(blob) != entry['record_sha256']:
            raise ValueError('record hash mismatch')
        r = json.loads(blob)
        timestamp = datetime.fromisoformat(r['recorded_at_utc'])
        local = timestamp.astimezone(ZoneInfo(protocol['timezone']))
        if (timestamp <= previous_time or local.date() != d or local.hour < 16 or
                d < date.fromisoformat(protocol['first_signal_date'])):
            raise ValueError('invalid recorded time boundary')
        if r['previous_record_sha256'] != previous_hash or r['protocol_sha256'] != meta['protocol_sha256']:
            raise ValueError('record chain mismatch')
        for name, sha in r['source_sha256'].items():
            raw = (folder/(name+'.csv')).read_bytes()
            if _sha(raw) != sha:
                raise ValueError('signal snapshot hash mismatch')
            _signal_rows(raw, d)
        previous_day, previous_hash, previous_time = d, entry['record_sha256'], timestamp
    actual = {p.name for p in (campaign/'records').iterdir()}
    if actual != {r['decision_date'] for r in journal}:
        raise ValueError('unjournaled or missing record directory; inspect interrupted write')
    return {'records':len(journal), 'last_signal_date':previous_day.isoformat() if previous_day else None,
            'head_sha256':previous_hash, 'last_recorded_at_utc':previous_time.isoformat(),
            'mode':'observation_only_no_statistical_go'}


def verify_campaign(campaign):
    campaign = Path(campaign)
    with _lock(campaign):
        return _verify(campaign)


def record_observation(campaign, *, repo=ROOT, now=None):
    actual_clock = now is None
    now = _clock(now)
    campaign = Path(campaign)
    with _lock(campaign):
        status = _verify(campaign)
        meta, p = _protocol(campaign)
        local = now.astimezone(ZoneInfo(p['timezone']))
        day = local.date()
        if day < date.fromisoformat(p['first_signal_date']) or local.hour < 16:
            raise ValueError('recording not open: same-day after 16:00 and on/after first signal day')
        folder = campaign/'records'/day.isoformat()
        if folder.exists():
            raise FileExistsError('this signal day is already recorded')
        if now <= datetime.fromisoformat(status['last_recorded_at_utc']):
            raise ValueError('clock precedes previous receipt')
        if status['last_signal_date'] and day.isoformat() <= status['last_signal_date']:
            raise ValueError('retroactive receipts are forbidden')
        for relative, sha in meta['code_sha256'].items():
            if _sha(_repo_file(repo, relative).read_bytes()) != sha:
                raise ValueError('frozen code/configuration changed: '+relative)
        raw = {name:_repo_file(repo, relative).read_bytes() for name, relative in p['sources'].items()}
        values = {name:_signal_rows(data, day) for name, data in raw.items()}
        revisions = []
        if status['last_signal_date']:
            prior_day = date.fromisoformat(status['last_signal_date'])
            prior = campaign/'records'/status['last_signal_date']
            for name in sorted(NAMES):
                old = _signal_rows((prior/(name+'.csv')).read_bytes(), prior_day)
                if any(values[name].get(d) != value for d, value in old.items()):
                    revisions.append(name)
        if actual_clock:
            receipt_time = _clock(None)
            if receipt_time.astimezone(ZoneInfo(p["timezone"])).date() != day or receipt_time < now:
                raise ValueError("date or clock changed during capture")
            now = receipt_time
        signals = {name:v[day.isoformat()] for name,v in values.items()}
        ew, slope = signals['equal_weight'], signals['slope20']
        record = {'decision_date':day.isoformat(), 'recorded_at_utc':now.isoformat(),
                  'fill':p['fill'], 'protocol_sha256':meta['protocol_sha256'],
                  'previous_record_sha256':status['head_sha256'],
                  'source_sha256':{name:_sha(data) for name,data in raw.items()},
                  'signals':signals, 'prior_signal_revisions':revisions,
                  'targets':{'spot_slope_longflat':int(slope>0),
                             'futures_ew_symmetric':int(ew>0)-int(ew<0),
                             'spot_ew_longflat_control':int(ew>0)}}
        folder.mkdir(exist_ok=False)
        for name, data in raw.items():
            (folder/(name+'.csv')).write_bytes(data)
        blob = _bytes(record)
        (folder/'record.json').write_bytes(blob)
        entry = {'decision_date':day.isoformat(), 'record_sha256':_sha(blob)}
        with (campaign/'journal.jsonl').open('a') as f:
            f.write(json.dumps(entry, sort_keys=True)+'\n')
            f.flush()
        return folder


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=['init', 'record', 'verify'])
    ap.add_argument('--campaign', type=Path, required=True)
    ap.add_argument('--protocol', type=Path)
    a = ap.parse_args(argv)
    try:
        if a.action == 'init':
            if not a.protocol:
                ap.error('init requires --protocol')
            init_campaign(a.campaign, a.protocol)
            result = verify_campaign(a.campaign)
        elif a.action == 'record':
            result = {'record':str(record_observation(a.campaign))}
        else:
            result = verify_campaign(a.campaign)
    except (ValueError, OSError) as exc:
        ap.exit(2, str(exc)+'\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
