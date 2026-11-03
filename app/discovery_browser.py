"""Read-only careers navigation and native Google/Apple results evidence.

Google keeps an accessibility announcement saying 'Loading job details' after
the listing cards have rendered. It is not a reliable readiness condition.
"""
import re
from urllib.parse import urljoin, urlsplit


def text_of(result):
    return '\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')


def page_url(text):
    match = re.search(r'(?m)^- Page URL: (\S+)', text)
    return match[1] if match else None


def public_google_page(url, *, results_only=False):
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'www.google.com', 'google.com'}:
        return False
    path = parsed.path.rstrip('/')
    board = '/about/careers/applications/jobs/results'
    return path == board or (not results_only and bool(re.fullmatch(re.escape(board) + r'/\d+[^/]*', path)))


def observed_destination(text, href):
    current = page_url(text)
    if not current:
        return None
    roots = re.findall(r'(?m)^\s*- /url: (/[^\s]*?/)\s*$', text)
    bases = [current, *[urljoin(current, root) for root in roots
                       if not root.startswith('//') and urlsplit(current).path.startswith(root)]]
    candidates = {urljoin(base, href) for base in bases}
    valid = [url for url in candidates if public_google_page(url)]
    return valid[0] if len(valid) == 1 else None


def observed_link(text, reference):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r'^\s*- link\b.*\[ref=' + re.escape(reference) + r'\]', line):
            continue
        indent = len(line) - len(line.lstrip())
        for child in lines[index + 1:]:
            if child.strip() and len(child) - len(child.lstrip()) <= indent:
                break
            href = re.search(r'^\s*- /url: (\S+)\s*$', child)
            if href:
                return observed_destination(text, href[1])
    return None


def job_links(text):
    return {url for href in re.findall(r'(?m)^\s*- /url: (\S+)\s*$', text)
            if (url := observed_destination(text, href)) and
            not public_google_page(url, results_only=True)}


def results_evidence(rpc, result, arguments=None):
    arguments = arguments or {}
    text = text_of(result)
    if (result.get('isError') or not public_google_page(page_url(text), results_only=True)
            or arguments.get('filename') or arguments.get('target')):
        return result
    expanded = False
    if 'depth' in arguments:
        # A shallow root tree can omit every card on this deeply nested SPA.
        result = rpc.call('browser_snapshot', {})
        if result.get('isError'):
            return result
        text = text_of(result); expanded = True
    links = job_links(text)
    if not links:
        # Wait for real results, never disappearance of a sticky announcement.
        ready = rpc.call('browser_wait_for', {'text': 'Showing'})
        if ready.get('isError'):
            return ready
        result = rpc.call('browser_snapshot', {})
        if result.get('isError'):
            return result
        text = text_of(result); links = job_links(text)
    if not links:
        return result
    paginator = re.search(r'(?m)^.*?(Showing [^\n]+)', text)
    note = (f'Discovery results evidence: {len(links)} distinct native Google job links are present. '
            + (paginator[1] + '. ' if paginator else '')
            + 'The results are not empty. A generic "Loading job details" announcement may remain '
              'after cards render; do not wait for that announcement to disappear. Use the observed '
              'job links/cards. Handle any actual dialog or permission checkpoint separately.')
    meta = {**result.get('_meta', {})}
    meta['ddoja'] = {**meta.get('ddoja', {}), 'google_results': {
        'native_job_links': len(links), 'depth_expanded': expanded, 'announcement_is_readiness_gate': False}}
    return {**result, 'content': [{'type': 'text', 'text': note}, *result.get('content', [])], '_meta': meta}


def apple_results_evidence(rpc, result, arguments=None):
    arguments = arguments or {}
    text = text_of(result)
    url = page_url(text)
    parsed = urlsplit(url or '')
    results_target = bool(arguments.get('target') and re.search(r'(?m)^- list "Job Opportunities"', text))
    if (result.get('isError') or parsed.scheme != 'https' or parsed.hostname != 'jobs.apple.com'
            or parsed.path.rstrip('/') != '/en-us/search'
            or arguments.get('filename') or (arguments.get('target') and not results_target)):
        return result
    links = set(re.findall(r'(?m)^\s*- /url: (/en-us/details/\S+)\s*$', text))
    expanded = False
    if not links:
        # Apple's result cards are nested below the shallow snapshot boundary.
        # Observe the actual complete tree, without navigating or changing tabs.
        result = rpc.call('browser_snapshot', {'target':arguments['target']} if results_target else {})
        if result.get('isError'):
            return result
        text = text_of(result)
        links = set(re.findall(r'(?m)^\s*- /url: (/en-us/details/\S+)\s*$', text))
        expanded = True
    if not links:
        return result
    note = (f'Discovery results evidence: {len(links)} distinct native Apple job-detail links are present. '
            'The search results are not empty; shallow snapshots can hide their nested cards. '
            'Use the observed detail links to read complete JDs, not just the expandable card summary. '
            'An inventory identity is not a captured JD. Continue processing the saved frontier; '
            'a single listing or ordinary page boundary is not a blocker or completion. '
            'Preserve exact controls, checkpoints and any actual permission or human-action gate.')
    meta = {**result.get('_meta', {})}
    meta['ddoja'] = {**meta.get('ddoja', {}), 'apple_results': {
        'native_job_links': len(links), 'depth_expanded': expanded}}
    return {**result, 'content': [{'type': 'text', 'text': note}, *result.get('content', [])], '_meta': meta}


def discovery_call(rpc, name, arguments):
    if name == 'browser_click' and not arguments.get('modifiers') and arguments.get('button', 'left') == 'left':
        reference = arguments.get('target') or arguments.get('ref')
        if reference:
            # Revalidate the actual current link. Never guess from a stale ref.
            before = rpc.call('browser_snapshot', {})
            if before.get('isError'):
                return before
            text = text_of(before)
            if public_google_page(page_url(text)):
                destination = observed_link(text, str(reference))
                if destination:
                    result = results_evidence(rpc, rpc.call('browser_navigate', {'url': destination}))
                    return {**result, 'content': [{'type': 'text', 'text':
                        'Opened the currently observed public Google Careers link with full navigation '
                        'in the same owned tab: ' + destination}, *result.get('content', [])],
                        '_meta': {**result.get('_meta', {}), 'ddoja_navigation': {
                            'requested_tool': name, 'actual_tool': 'browser_navigate', 'observed_url': destination}}}
    result = rpc.call(name, arguments)
    if name == 'browser_tabs' and not result.get('isError'):
        # A tab inventory is not a page observation. Return a current native
        # snapshot too, so a harmless list/select cannot erase owned-page
        # evidence at the following discovery checkpoint.
        snapshot = rpc.call('browser_snapshot', {})
        snapshot = apple_results_evidence(rpc, results_evidence(rpc, snapshot))
        return {**snapshot, 'content': [*result.get('content', []), *snapshot.get('content', [])]}
    if name in {'browser_snapshot', 'browser_navigate', 'browser_navigate_back', 'browser_click', 'browser_wait_for'}:
        scoped = arguments if name == 'browser_snapshot' else {}
        return apple_results_evidence(rpc, results_evidence(rpc, result, scoped), scoped)
    return result
