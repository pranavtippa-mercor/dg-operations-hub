#!/usr/bin/env python3
"""Read-only, deterministic Slack evidence collection for the operations hub.

Runtime inputs and output are private. Full canonical threads provide confirmation
evidence; cursor-paginated exact task-reference searches provide wider activity.
No message, Studio mutation, relative-date inference, or semantic model call occurs.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
PT = ZoneInfo('America/Los_Angeles')
THREAD_LIMIT = 1000  # The connector exposes no thread cursor: reaching this is incomplete.
MSG = re.compile(r'^\[([^\n]+?) · (\d+\.\d+)\] ', re.M)
TASK_TOKEN = re.compile(r'(?<![A-Z0-9-])DOP(?:2)?-[A-Z0-9]+(?:-[A-Z0-9]+)+(?![A-Z0-9-])', re.I)
TASK_ID = re.compile(r'\btask_[a-zA-Z0-9]+\b')
NO_ASK = re.compile(r'no confirmation reply is needed', re.I)
ASK = re.compile(r'(?:confirm[^.\n]{0,100}\b(?:meet (?:that|the) (?:deadline|date)|complete this task|by\b)|'
                 r'can you commit to getting|confirm in this thread whether you can meet)', re.I)
NEGATIVE = re.compile(r"\b(?:can'?t|cannot|won'?t|unable|not able|at risk|slip|delay|blocked|"
                      r'need more time|extension|reassign|hand(?:ing)? (?:it |this )?off)\b', re.I)
AFFIRMATIVE = re.compile(r"(?:^\W*(?:yes|yep|confirmed|confirming)\b|\bi (?:can|will) (?:meet|hit|finish|complete|deliver)\b|"
                        r"\b(?:i'll|i will) (?:have|get) (?:it|this|them)\b.{0,50}\b(?:done|ready|by)\b|"
                        r'\b(?:deadline|date|timeline) works\b|\bcan meet (?:that|the) (?:deadline|date)\b|\bi commit\b)', re.I)
HEDGE = re.compile(r'\b(?:if|hope|hopefully|try|trying|aim|aiming|might|maybe|probably|should|not sure)\b|\?', re.I)
PARTIAL = re.compile(r'\b(?:stage\s*[12]|submit(?:ted)? for review|ready for (?:review|audit))\b', re.I)
MONTHS = {name: i for i, name in enumerate(('january','february','march','april','may','june','july','august','september','october','november','december'), 1)}


def stamp(value=None):
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace('+00:00', 'Z')


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def load(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else copy.deepcopy(default)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
    temporary.replace(path)
    os.chmod(path, 0o600)


def decoded(value):
    """Accept SourceClient output including its multi-content JSON-lines response."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError('Unsupported Slack response')
    try:
        result = json.loads(value)
        if isinstance(result, dict):
            return result
    except ValueError:
        pass
    documents = []
    for line in value.splitlines():
        try:
            document = json.loads(line)
            if isinstance(document, dict):
                documents.append(document)
        except ValueError:
            continue
    for document in documents:
        if 'results' in document or 'pagination_info' in document:
            return document
    return {'transcript': value}


def thread_messages(value, root_ts, limit=THREAD_LIMIT):
    doc = decoded(value)
    transcript = doc.get('transcript') or doc.get('result')
    if not isinstance(transcript, str):
        raise ValueError('Slack thread did not return a transcript')
    hits = list(MSG.finditer(transcript))
    messages = [{'author': hit.group(1).strip(), 'ts': hit.group(2),
                 'text': transcript[hit.end():hits[i + 1].start() if i + 1 < len(hits) else len(transcript)].strip()}
                for i, hit in enumerate(hits)]
    if not messages or messages[0]['ts'] != root_ts:
        raise ValueError('Canonical thread root absent; complete read not established')
    if len(messages) >= limit or doc.get('has_more') or doc.get('next_cursor'):
        raise ValueError('Thread reaches connector limit; no thread-pagination interface is available')
    if re.search(r'(?:\[truncated\]|output truncated|more messages omitted)', transcript, re.I):
        raise ValueError('Truncated thread transcript')
    times = [float(m['ts']) for m in messages]
    if len(set(times)) != len(times) or times != sorted(times):
        raise ValueError('Invalid thread chronology')
    return messages


def parse_search(value):
    doc = decoded(value)
    results, pagination = doc.get('results'), doc.get('pagination_info')
    if not isinstance(results, str) or not isinstance(pagination, str):
        raise ValueError('Search pagination evidence missing')
    cursor = re.search(r'cursor\s+`([^`]+)`', pagination)
    if not cursor and not re.search(r'(?:End of results|No more pages)', pagination, re.I):
        raise ValueError('Unrecognized search pagination state')
    messages = []
    for part in re.split(r'^### Result \d+ of \d+\s*\n', results, flags=re.M)[1:]:
        channel = re.search(r'^Channel: .*?\(ID: ([A-Z0-9]+)\)', part, re.M)
        channel_name = re.search(r'^Channel: #([^\n]+?)\s*\(ID:', part, re.M)
        author = re.search(r'^From: (.*?) \(ID: ([A-Z0-9]+)\)', part, re.M)
        timestamp = re.search(r'^Message_ts: (\d+\.\d+)', part, re.M)
        link = re.search(r'^Permalink: \[link\]\((https://[^\s)]+)\)', part, re.M)
        body = re.search(r'^Text:\s*\n([\s\S]*)', part, re.M)
        if not all((channel, author, timestamp, link, body)):
            raise ValueError('Search result missing evidence fields')
        text = re.sub(r'\n---\s*$', '', body.group(1)).strip()
        if re.search(r'(?:\[truncated\]|output truncated)', text, re.I):
            raise ValueError('Truncated search result')
        person = re.sub(r'\s*<[^>]+>\s*$', '', author.group(1)).strip()
        messages.append({'channel': channel.group(1), 'channel_name': channel_name.group(1).strip() if channel_name else None, 'author': person,
                         'author_id': author.group(2), 'ts': timestamp.group(1),
                         'url': link.group(1), 'text': text,
                         'bot': '[BOT]' in part.split('Time:', 1)[0]})
    if not messages and not re.search(r'No results found', results, re.I):
        raise ValueError('Search parser could not verify returned records')
    return messages, cursor.group(1) if cursor else None


def search_all(client, workspace, query, *, after=None, before=None, max_pages=100):
    messages, cursors, cursor = {}, set(), None
    for page in range(max_pages):
        args = {'workspace': workspace, 'query': query, 'sort': 'timestamp', 'sort_dir': 'desc',
                'limit': 20, 'include_bots': True, 'include_context': False,
                'response_format': 'detailed', 'max_context_length': 12000}
        if after is not None:
            args['after'] = str(int(after))
        if before is not None:
            args['before'] = str(int(before))
        if cursor:
            args['cursor'] = cursor
        batch, next_cursor = parse_search(client.call('slack_search_public_and_private', args))
        for message in batch:
            timestamp = float(message['ts'])
            if after is not None and timestamp < after - 1:
                raise ValueError('Search ignored the requested lower time bound')
            if before is not None and timestamp > before + 1:
                raise ValueError('Search ignored the requested upper time bound')
            messages[(message['channel'], message['ts'])] = message
        if not next_cursor:
            return list(messages.values()), page + 1
        if next_cursor in cursors:
            raise ValueError('Search pagination repeated its cursor')
        cursors.add(next_cursor)
        cursor = next_cursor
    raise ValueError('Search pagination cap reached; watermark retained')


def root_from_url(url):
    if not url:
        return None
    parsed = urlparse(url)
    hit = re.search(r'/archives/([A-Z0-9]+)/p(\d+)', parsed.path)
    if not hit:
        return None
    ts = parse_qs(parsed.query).get('thread_ts', [None])[0]
    if not ts:
        digits = hit.group(2)
        ts = digits[:-6] + '.' + digits[-6:]
    return {'channel': hit.group(1), 'ts': ts}


def message_url(root, ts):
    return f"https://mercor.enterprise.slack.com/archives/{root['channel']}/p{ts.replace('.', '')}?thread_ts={root['ts']}&cid={root['channel']}"


def clean_name(value):
    value = re.sub(r'\[[^\]]*\]', '', value or '')
    value = re.split(r'\s+[—–-]\s+', value)[0]
    return re.sub(r'\s+', ' ', value).strip().casefold()


def addressee(text):
    prefix = text[:250]
    mention = re.search(r'<@([UW][A-Z0-9]+)(?:\|([^>]+))?>', prefix)
    if mention:
        return mention.group(2), mention.group(1)
    mention = re.search(r'(?:\bHi[,\s]*|^\s*|:wave:\s*)@([^\n,<>—]+?)(?=\s*(?:—|--|,|\n|$))', prefix)
    return (mention.group(1).strip(), None) if mention else (None, None)


def is_person(message, name, identifier=None, aliases=None):
    if identifier and message.get('author_id'):
        return message['author_id'] == identifier
    if identifier and re.fullmatch(r'[UW][A-Z0-9]+', message['author']):
        return message['author'] == identifier
    accepted = {clean_name(name)} - {''}
    if identifier:
        accepted |= {clean_name(x) for x in (aliases or {}).get(identifier, [])}
    return clean_name(message['author']) in accepted


def classify_reply(text):
    text = re.sub(r'<https?://[^>]+>', '', text)
    if NEGATIVE.search(text):
        return 'at_risk'
    if AFFIRMATIVE.search(text[:500]) and not HEDGE.search(text[:500]) and not PARTIAL.search(text[:500]):
        return 'confirmed'
    return 'replied'


def explicit_deadline(text):
    """Return one unambiguous absolute date AND timezone/time; otherwise abstain."""
    plain = re.sub(r'[*_`]', '', text)
    clocks = re.findall(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', plain, re.I)
    clock_values = {(int(hour) % 12, int(minute or 0), period.lower()) for hour, minute, period in clocks}
    if re.search(r'\bnoon\b', plain, re.I): clock_values.add((0, 0, 'p'))
    if re.search(r'\bmidnight\b', plain, re.I): clock_values.add((0, 0, 'a'))
    if re.search(r'\b(?:EOD|end of day)\b', plain, re.I): clock_values.add((11, 59, 'p'))
    if len(clock_values) > 1:
        return None
    iso_match = re.findall(r'\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})\b', plain)
    if len(set(iso_match)) == 1:
        remainder = plain.replace(iso_match[0], '')
        if clocks or re.search(r'\b(?:' + '|'.join(MONTHS) + r')\s+\d|\b\d{4}-\d{2}-\d{2}\b', remainder, re.I):
            return None
        try:
            return stamp(parse_time(iso_match[0]))
        except ValueError:
            return None
    if iso_match:
        return None
    date_hits = list(re.finditer(r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b', plain, re.I))
    if len(date_hits) != 1 or re.search(r'\b\d{4}-\d{2}-\d{2}\b', plain):
        return None
    hit = date_hits[0]
    tail = plain[hit.end():hit.end() + 110]
    if re.search(r'\b(?:between|either|around|approximately|sometime)\b|\b\d{1,2}(?::\d{2})?\s*(?:[-–—]|or|and|to)\s*\d{1,2}', tail, re.I):
        return None
    zone = re.search(r'\b(Pacific|PT|PDT|PST|UTC|GMT)\b', tail, re.I)
    if not zone:
        return None
    clock = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', tail, re.I)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        if not 1 <= hour <= 12 or minute >= 60:
            return None
        hour = hour % 12 + (12 if clock.group(3).lower() == 'p' else 0)
    elif re.search(r'\b(?:EOD|end of day)\b', tail, re.I):
        hour, minute = 23, 59
    elif re.search(r'\bnoon\b', tail, re.I):
        hour, minute = 12, 0
    else:
        return None
    try:
        value = datetime(int(hit.group(3)), MONTHS[hit.group(1).lower()], int(hit.group(2)), hour, minute,
                         tzinfo=UTC if zone.group(1).upper() in ('UTC', 'GMT') else PT)
    except ValueError:
        return None
    preceding = plain[max(0, hit.start() - 15):hit.start()]
    weekday = re.search(r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*$', preceding, re.I)
    if weekday and weekday.group(1).lower() != value.strftime('%A').lower():
        return None
    if zone.group(1).upper() in ('PST', 'PDT') and value.tzname() != zone.group(1).upper():
        return None
    return stamp(value)


def requested_deadline(text):
    hit = re.search(r'\b(?:Ready for Delivery|RFD)\s+(?:deadline\s*:|by)\s*([\s\S]{1,180})', re.sub(r'[*_`]', '', text), re.I)
    if not hit:
        return None, None
    phrase = re.split(r'\bincluding\b|\bPlease reply\b|\n', hit.group(1), maxsplit=1, flags=re.I)[0].strip().rstrip('.,')
    return explicit_deadline(phrase), phrase


def confirmation(messages, root, owner, observed_at, aliases=None):
    asks = [i for i, message in enumerate(messages) if ASK.search(message['text']) and not NO_ASK.search(message['text'])]
    if not asks:
        return {'state': 'no_request', 'messages': len(messages), 'observed_at': observed_at,
                'scope': 'complete canonical task thread', 'complete': True}
    index = asks[-1]
    ask = messages[index]
    name, identity = addressee(ask['text'])
    # Current owner is a fallback only when the request did not identify somebody.
    if not name and not identity:
        name = owner
    due, phrase = requested_deadline(ask['text'])
    rec = {'state': 'awaiting', 'asked_at': ask['ts'], 'asked_by': ask['author'],
           'asked_url': message_url(root, ask['ts']), 'asked_text': ask['text'][:2000],
           'deadline': phrase, 'deadline_at': due, 'writer': name, 'writer_id': identity,
           'messages': len(messages), 'observed_at': observed_at, 'scope': 'complete canonical task thread', 'complete': True}
    replies = [m for m in messages[index + 1:] if is_person(m, name, identity, aliases)]
    selected = None
    for reply in replies:
        state = classify_reply(reply['text'])
        if selected is None or state in ('confirmed', 'at_risk'):
            selected = reply
    if selected:
        rec.update(reply_at=selected['ts'], reply_by=selected['author'],
                   reply_url=message_url(root, selected['ts']), reply_text=selected['text'][:2000],
                   state=classify_reply(selected['text']), by_addressee=True)
    elif messages[index + 1:] and not name:
        rec.update(state='not_checked', attribution_unresolved=True)
    return rec


def update_commitment(record, conf):
    """Preserve history; never turn a new reply into an operational date override."""
    rec = copy.deepcopy(record or {})
    if conf.get('state') != 'confirmed' or not conf.get('by_addressee'):
        if conf.get('state') == 'at_risk' and rec.get('committed'):
            rec['commitment_review'] = {'state': 'later_at_risk_reply', 'at': conf.get('reply_at'), 'url': conf.get('reply_url')}
        return rec
    reply = conf.get('reply_text', '')
    due = explicit_deadline(reply)
    # A clear answer to a single proven full-RFD date inherits only that ask.
    if not due and conf.get('deadline_at') and not re.search(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|tomorrow|today|tonight|next|weekend)\b|\d', reply, re.I):
        due = conf['deadline_at']
    if not due:
        rec['commitment_review'] = {'state': 'unresolved_absolute_date', 'at': conf.get('reply_at'), 'url': conf.get('reply_url')}
        return rec
    source_at = stamp(datetime.fromtimestamp(float(conf['reply_at']), UTC))
    prior = rec.get('committed')
    # Historical full-thread reads must not overwrite a newer captured commitment.
    prior_at = (prior or {}).get('source_at') or (prior or {}).get('captured_at')
    if prior_at and parse_time(prior_at) > parse_time(source_at):
        return rec
    if prior and prior.get('url') == conf.get('reply_url') and prior.get('due') == due:
        return rec
    if prior:
        rec.setdefault('commitment_history', []).append(prior)
        if prior.get('due') != due:
            rec.setdefault('moves', []).append({'from': prior.get('due'), 'to': due, 'at': source_at,
                                                'source': 'thread', 'by': conf.get('reply_by'), 'url': conf.get('reply_url')})
    rec['committed'] = {'due': due, 'source': 'thread', 'by': conf.get('reply_by'), 'url': conf.get('reply_url'),
                        'quote': reply, 'captured_at': conf['observed_at'], 'source_at': source_at,
                        'basis': 'explicit_absolute_reply' if explicit_deadline(reply) else 'clear_confirmation_of_explicit_RFD_ask'}
    rec.pop('commitment_review', None)
    return rec


def explicit_owner_commitment(messages, root, owner, observed_at, aliases=None):
    """Recognize a volunteered full-RFD commitment without inventing an ask."""
    for message in reversed(messages):
        if (is_person(message, owner, aliases=aliases) and classify_reply(message['text']) == 'confirmed'
                and re.search(r'\b(?:Ready for Delivery|RFD|fully complete)\b', message['text'], re.I)
                and explicit_deadline(message['text'])):
            return {'state': 'confirmed', 'by_addressee': True, 'reply_at': message['ts'],
                    'reply_by': message['author'], 'reply_url': message_url(root, message['ts']),
                    'reply_text': message['text'], 'observed_at': observed_at}
    return None


def confirmation_fingerprint(record):
    """Archive changes in evidence, not periodic freshness/message-count bumps."""
    keys = ('state', 'asked_at', 'asked_by', 'asked_url', 'deadline', 'deadline_at', 'writer', 'writer_id',
            'reply_at', 'reply_by', 'reply_text', 'reply_url', 'by_addressee', 'thread_gap', 'attribution_unresolved')
    return {key: record.get(key) for key in keys}


def tracked_tasks(task_state):
    seed = task_state.get('seed', {})
    statuses = task_state.get('statuses', {})
    owners = {uid: value.get('name', '') if isinstance(value, dict) else value for uid, value in task_state.get('users', {}).items()}
    owners.update(seed.get('owners', {}))
    tasks = {}
    for row in task_state.get('rows', []):
        name = row.get('task_name', '')
        if re.search(r'TEST_ONLY|SYNTHETIC_CANARY|BASELINE_G2', name) or re.match(r'^(TEST[-_]|RUBRIC-)', name):
            continue
        mode, artifact = row.get('task_mode'), row.get('artifact')
        if not ((mode == 'Edit Eval' and artifact in ('html', 'docx', 'pptx', 'xlsx')) or
                (mode == 'Base Task' and name.startswith('DOP-TC26-BASE-'))):
            continue
        definition = statuses.get(row.get('status'), '')
        stage = definition.get('name', '') if isinstance(definition, dict) else definition
        if stage == 'Discarded':
            continue
        tasks[name] = {'id': row['task_id'], 'name': name, 'owner': owners.get(row.get('owned_by'))}
    if not tasks:
        raise ValueError('Private task state has no verified tracked roster')
    return tasks


def referenced_tasks(message, tasks):
    tokens = {s.upper() for s in TASK_TOKEN.findall(message['text'])}
    identifiers = set(TASK_ID.findall(message['text']))
    matched = []
    for name, task in tasks.items():
        alias = re.sub(r'-TEMPLATE$', '', name)
        if (name.upper() in tokens or task['id'] in identifiers
                or alias != name and alias not in tasks and alias.upper() in tokens):
            matched.append(name)
    return matched


def collect(config, client=None, *, only=None, limit=None, now=None):
    from source_client import SourceClient
    from runtime_config import normalize_config
    config = normalize_config(config, root=ROOT)
    settings = config['slack']
    cache = Path(settings['cache_dir'])
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(cache, 0o700)
    with (cache/'collector.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Slack collector is already running')
        owned_client = client is None
        client = client or SourceClient(config, upstream_tools=['slack_read_thread', 'slack_search_public_and_private'])
        try:
            return _collect(config, client, only=only, limit=limit, now=now)
        finally:
            if owned_client:
                client.close()


def _collect(config, client, *, only=None, limit=None, now=None):
    settings, task_state = config['slack'], load(config['task_state'])
    cache, workspace = Path(settings['cache_dir']), settings.get('workspace', 'mercor')
    started = now or datetime.now(UTC)
    observed = stamp(started)
    tasks = tracked_tasks(task_state)
    roots_doc = load(cache/'roots.json', {'roots': {}})
    roots = roots_doc.setdefault('roots', {})
    for name, source in task_state.get('fixed_html', {}).items():
        root = root_from_url(source.get('slack'))
        if root:
            roots.setdefault(name, root)
    for name in tasks:
        alias = re.sub(r'-TEMPLATE$', '', name)
        if name not in roots and alias in roots:
            roots[name] = copy.deepcopy(roots[alias])
    old = load(cache/'state.json', {})
    confirmations = load(cache/'confirmations.json', {'tasks': {}})
    activity = load(cache/'activity.json', {})
    commitments = load(cache/'commitments.json', {})
    selected = sorted(tasks)
    if only:
        selected = [name for name in selected if name in set(only)]
        if len(selected) != len(set(only)):
            raise ValueError('Requested task is outside the current verified roster')
    if limit is not None:
        selected = selected[:limit]
    partial = len(selected) != len(tasks)
    errors, missing, candidates, pages = {}, [], {}, 0
    discovery_reused, reference_keys = 0, {}
    discovery_state = roots_doc.setdefault('_discovery', {})
    search_messages, search_complete = [], False
    earliest = parse_time(old['mention_through']) - timedelta(minutes=5) if old.get('mention_through') else started - timedelta(hours=settings.get('initial_lookback_hours', 24))
    try:
        search_messages, pages = search_all(client, workspace, 'DOP*', after=earliest.timestamp(), before=started.timestamp(),
                                            max_pages=settings.get('max_search_pages', 100))
        search_complete = True
    except Exception as exc:
        errors['mentions'] = str(exc)[:250]
    selected_set = set(selected)
    # The search spans task references, not all company content. Retain only
    # exact current task names or native IDs, including references in other rooms.
    for message in search_messages:
        for name in referenced_tasks(message, tasks):
            if name not in selected_set:
                continue
            reference_keys.setdefault(name, set()).add(message['channel'] + ':' + message['ts'])
            candidate = root_from_url(message['url'])
            if candidate:
                candidates.setdefault(name, {})[(candidate['channel'], candidate['ts'])] = candidate
            update_activity(activity, name, message, observed, 'exact task reference search')
    identity = {(m['channel'], m['ts']): m for m in search_messages}
    threads, thread_observations, thread_checked = {}, {}, {}
    channels = roots_doc.get('_channels', {})
    for message in search_messages:
        if message.get('channel_name'):
            channels[message['channel']] = message['channel_name']
    roots_doc['_channels'] = channels
    channel_complete, changed_roots, ambiguous_channels = set(), set(), set()
    known_root_keys = {(root['channel'], root['ts']) for root in roots.values()}
    channel_pages = 0
    baseline_at = old.get('collected_at')
    if baseline_at and not partial:
        # A complete channel search includes replies without a task name. It
        # permits a cache reuse only for unaffected canonical roots. Full reads
        # still recur daily to detect edited/deleted historical messages.
        selected_channels = {roots[name]['channel'] for name in selected if name in roots}
        for channel in sorted(selected_channels):
            channel_name = channels.get(channel)
            if not channel_name:
                continue
            # A baseline may take minutes. Start at its source cutoff, not its
            # finish time, so replies posted after an early thread read survive.
            fallback_cutoff = min(old.get('mention_through', baseline_at), baseline_at)
            since = parse_time(old.get('channel_through', {}).get(channel, fallback_cutoff)) - timedelta(minutes=5)
            try:
                batch, count = search_all(client, workspace, 'in:' + channel_name,
                                          after=since.timestamp(), before=started.timestamp(),
                                          max_pages=settings.get('max_channel_pages', 100))
                if any(message['channel'] != channel for message in batch):
                    raise ValueError('Channel search returned a different channel')
                channel_pages += count
                channel_complete.add(channel)
                for message in batch:
                    candidate = root_from_url(message['url'])
                    if candidate:
                        changed_roots.add((candidate['channel'], candidate['ts']))
                        if not parse_qs(urlparse(message['url']).query).get('thread_ts') and (candidate['channel'], candidate['ts']) not in known_root_keys:
                            # Without a parent pointer this may be an unmentioned
                            # reply to any tracked root in the channel.
                            ambiguous_channels.add(channel)
                    identity[(message['channel'], message['ts'])] = message
                    for name in referenced_tasks(message, tasks):
                        if name in selected_set:
                            update_activity(activity, name, message, observed, 'complete incremental project-channel search')
            except Exception as exc:
                # A failed channel query falls back to full canonical reads;
                # it never grants permission to reuse its cached threads.
                errors['channel:' + channel] = str(exc)[:250]
    # Global exact-name activity also invalidates relevant canonical caches.
    for message in search_messages:
        candidate = root_from_url(message['url'])
        if candidate:
            changed_roots.add((candidate['channel'], candidate['ts']))
    reused, full_reads = 0, 0
    thread_cached = {}

    def read(root):
        nonlocal reused, full_reads
        key = (root['channel'], root['ts'])
        if key not in threads:
            filename = root['channel'] + '-' + root['ts'].replace('.', '_') + '.json'
            previous = load(cache/'threads'/filename, {})
            can_reuse = (baseline_at and search_complete and root['channel'] in channel_complete and root['channel'] not in ambiguous_channels and key not in changed_roots
                         and previous.get('complete') and previous.get('root', {}).get('ts') == root['ts']
                         and previous.get('observed_at')
                         and (started - parse_time(previous['observed_at'])).total_seconds() < settings.get('full_thread_interval_hours', 24) * 3600)
            if can_reuse:
                messages = previous['messages']
                checked_at = observed if now is not None else stamp()
                observed_at = previous['observed_at']
                reused += 1
            else:
                full_reads += 1
                raw = client.call('slack_read_thread', {'workspace': workspace, 'channel_id': root['channel'], 'message_ts': root['ts'], 'limit': THREAD_LIMIT})
                messages = thread_messages(raw, root['ts'])
                observed_at = checked_at = observed if now is not None else stamp()
            for message in messages:
                evidence = identity.get((root['channel'], message['ts']))
                if evidence:
                    message['author_id'] = evidence['author_id']
                message['url'] = message_url(root, message['ts'])
                message['channel'] = root['channel']
            threads[key] = messages
            thread_cached[key] = bool(can_reuse)
            thread_observations[key] = observed_at
            thread_checked[key] = checked_at
            atomic_json(cache/'threads'/filename, {'observed_at': observed_at, 'checked_at': checked_at,
                                                 'check_mode': 'complete incremental channel search' if can_reuse else 'full thread read',
                                                 'root': root, 'complete': True, 'messages': messages})
        return threads[key]

    completed, canonical_reused = 0, 0
    for name in selected:
        task = tasks[name]
        root = roots.get(name)
        try:
            if not root:
                prior_discovery = discovery_state.get(name, {})
                roster_key = {'id': task['id'], 'name': name, 'owner': task.get('owner')}
                if (search_complete and prior_discovery.get('complete') and prior_discovery.get('roster') == roster_key
                        and prior_discovery.get('outcome') == 'no_unique_root' and prior_discovery.get('discovered_at')
                        and 0 <= (started - parse_time(prior_discovery['discovered_at'])).total_seconds() < settings.get('discovery_interval_hours', 24) * 3600
                        and reference_keys.get(name, set()).issubset(set(prior_discovery.get('references', [])))):
                    missing.append(name)
                    discovery_reused += 1
                    prior_discovery['checked_at'] = observed
                    confirmations.setdefault('tasks', {}).setdefault(name, {'state': 'not_checked'})['thread_gap'] = 'No unique dedicated thread verified'
                    continue
                # New and previously unmatched tasks get an exact-name paginated
                # discovery pass, so their root need not have appeared recently.
                discovered, discovery_pages = search_all(client, workspace, '"' + name + '"', max_pages=settings.get('max_discovery_pages', 20))
                pages += discovery_pages
                for message in discovered:
                    if name not in referenced_tasks(message, tasks):
                        continue
                    update_activity(activity, name, message, observed, 'exact task reference search')
                    reference_keys.setdefault(name, set()).add(message['channel'] + ':' + message['ts'])
                    candidate = root_from_url(message['url'])
                    if candidate:
                        candidates.setdefault(name, {})[(candidate['channel'], candidate['ts'])] = candidate
                matches = []
                possible = list(candidates.get(name, {}).values())
                if len(possible) > settings.get('max_candidate_roots', 40):
                    raise ValueError('Root-discovery candidate limit reached; canonical mapping remains unverified')
                for candidate in possible:
                    initial = read(candidate)[0]
                    refs = referenced_tasks(initial, tasks)
                    # A canonical root is a short, single-task heading/link, not
                    # a multi-task report that happened to mention this task.
                    if refs == [name] and len(initial['text']) < 1200 and (task['id'] in initial['text'] or initial['text'].strip('*_ :thread:') == name):
                        matches.append(candidate)
                discovery_state[name] = {'complete': True, 'roster': roster_key, 'discovered_at': observed, 'checked_at': observed,
                                         'references': sorted(reference_keys.get(name, set())),
                                         'outcome': 'verified_root' if len(matches) == 1 else 'no_unique_root'}
                if len(matches) == 1:
                    root = roots[name] = {**matches[0], 'verified_at': observed, 'method': 'exact dedicated root evidence'}
                else:
                    missing.append(name)
                    confirmations.setdefault('tasks', {}).setdefault(name, {'state': 'not_checked'})
                    confirmations['tasks'][name]['thread_gap'] = 'No unique dedicated thread verified'
                    continue
            messages = read(root)
            checked_at = thread_observations[(root['channel'], root['ts'])]
            rec = confirmation(messages, root, task['owner'], checked_at, settings.get('identity_aliases'))
            rec['checked_at'] = thread_checked[(root['channel'], root['ts'])]
            prior = confirmations.setdefault('tasks', {}).get(name)
            if prior and confirmation_fingerprint(prior) != confirmation_fingerprint(rec):
                archive_key = hashlib.sha256((name + observed).encode()).hexdigest()[:20]
                atomic_json(cache/'history'/('confirmation-' + archive_key + '.json'), {'task': name, 'replaced_at': observed, 'record': prior})
            confirmations['tasks'][name] = rec
            if messages:
                update_activity(activity, name, messages[-1], checked_at, 'canonical task thread')
            commitments[name] = update_commitment(commitments.get(name), rec)
            volunteered = explicit_owner_commitment(messages, root, task['owner'], checked_at, settings.get('identity_aliases'))
            if volunteered:
                commitments[name] = update_commitment(commitments.get(name), volunteered)
            completed += 1
            canonical_reused += int(thread_cached[(root['channel'], root['ts'])])
        except Exception as exc:
            errors[name] = str(exc)[:250]
        if completed and completed % 25 == 0:
            print(json.dumps({'progress': completed, 'scope': len(selected), 'errors': len(errors)}), file=sys.stderr, flush=True)
    finished = stamp() if now is None else observed
    scope_complete = not partial and not errors
    state = copy.deepcopy(old)
    state.update(attempted_at=observed, finished_at=finished,
                 coverage={'tasks': len(tasks), 'attempted': len(selected), 'threads_complete': completed,
                           'threads_reread': completed - canonical_reused, 'threads_reused': canonical_reused,
                           'thread_rpc_calls': full_reads, 'cached_thread_reads': reused,
                           'channels_incrementally_checked': len(channel_complete), 'full_thread_interval_hours': settings.get('full_thread_interval_hours', 24),
                           'missing_roots': len(missing), 'root_discoveries_reused': discovery_reused, 'errors': len(errors), 'partial_selection': partial,
                           'mentions_complete': search_complete, 'mention_window_start': stamp(earliest),
                           'mention_window_end': observed}, errors=errors, missing_roots=missing)
    if scope_complete:
        state['mention_through'] = observed
    if scope_complete:
        state['collected_at'] = observed
        state['checked_through'] = observed
        state['channel_through'] = {root['channel']: observed for name, root in roots.items() if name in selected_set}
    confirmations.update(attempted_at=observed, errors=errors, missing_roots=missing,
                         source='complete canonical task threads; read-only deterministic collection', coverage=state['coverage'])
    if scope_complete:
        timestamps = [confirmations['tasks'][name].get('observed_at') for name in selected if name not in missing]
        confirmations['harvested_at'] = min((value for value in timestamps if value), default=finished)
        confirmations['checked_at'] = observed
    # Preserve private channel metadata/config edits made while a long read ran.
    latest_roots = load(cache/'roots.json', {})
    channels.update(latest_roots.get('_channels', {}))
    latest_roots.update(roots_doc)
    latest_roots['_channels'] = channels
    roots_doc = latest_roots
    for filename, document in [('roots.json', roots_doc), ('activity.json', activity), ('commitments.json', commitments), ('confirmations.json', confirmations)]:
        atomic_json(cache/filename, document)
    atomic_json(cache/'state.json', state)  # Commit marker follows all sidecars.
    atomic_json(cache/'bundle.json', {'roots': roots, 'activity': activity, 'commitments': commitments,
                                    'confirmations': confirmations, 'state': state})
    return {'ok': scope_complete, 'attempted_at': observed, 'completed_at': finished,
            'collected_at': state.get('collected_at'), 'tasks': len(tasks), 'attempted': len(selected),
            'threads_complete': completed, 'missing_roots': len(missing), 'root_discoveries_reused': discovery_reused, 'errors': len(errors),
            'mention_search_complete': search_complete, 'search_pages': pages,
            'channel_search_pages': channel_pages, 'threads_reread': completed - canonical_reused, 'threads_reused': canonical_reused,
                           'thread_rpc_calls': full_reads, 'cached_thread_reads': reused,
            'partial_selection': partial, 'runtime_dependency': 'private hub state + authenticated read-only source client'}


def update_activity(activity, name, message, observed, scope):
    at = stamp(datetime.fromtimestamp(float(message['ts']), UTC))
    old = activity.get(name)
    if old and old.get('at') and parse_time(old['at']) > parse_time(at):
        return
    activity[name] = {'at': at, 'by': message['author'], 'url': message.get('url', ''),
                      'text': message['text'][:1200], 'seen_at': observed, 'scope': scope}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT/'.private/config.json'))
    parser.add_argument('--only', action='append', help='Bounded test scope; never advances whole-scope freshness')
    parser.add_argument('--limit', type=int, help='Bounded test scope; never advances whole-scope freshness')
    args = parser.parse_args()
    from runtime_config import load_config
    result = collect(load_config(args.config), only=args.only, limit=args.limit)
    print(json.dumps(result))
    return 0 if result['ok'] or result['partial_selection'] and not result['errors'] else 2


if __name__ == '__main__':
    sys.exit(main())
