import pytest

from app.discovery_browser import discovery_call, results_evidence, text_of
from tools.browser_mcp import BrowserBridge


BOARD = 'https://www.google.com/about/careers/applications/jobs/results?location=United%20States'
DETAIL = 'https://www.google.com/about/careers/applications/jobs/results/123-software-engineer?location=United%20States'


def snapshot(url=BOARD, *, cards=True, notice=True):
    text = '### Page\n- Page URL: ' + url + '\n### Snapshot\n'
    text += '- link "Careers" [ref=root]:\n  - /url: /about/careers/applications/\n'
    if cards:
        text += ('- link "Learn more about Software Engineer" [ref=job123]:\n'
                 '  - /url: jobs/results/123-software-engineer?location=United%20States\n'
                 '- generic [ref=page]: Showing 1 to 20 of 1836 rows\n')
    if notice:
        text += '- generic [ref=announce]: Loading job details\n'
    return {'content': [{'type': 'text', 'text': text}]}


class RPC:
    def __init__(self, responses):
        self.responses = list(responses); self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        assert self.responses, 'Unexpected browser operation'
        return self.responses.pop(0)


def test_shallow_snapshot_cannot_hide_rendered_cards_behind_sticky_announcement():
    shallow = snapshot(cards=False)
    full = snapshot()
    rpc = RPC([shallow, full])
    result = discovery_call(rpc, 'browser_snapshot', {'depth': 3})
    assert rpc.calls == [('browser_snapshot', {'depth': 3}), ('browser_snapshot', {})]
    assert result['_meta']['ddoja']['google_results']['native_job_links'] == 1
    assert 'results are not empty' in text_of(result)
    assert 'Loading job details' in text_of(result)  # Native evidence is retained.
    assert 'ref=job123' in text_of(result)


def test_initial_empty_results_wait_for_actual_cards_once_without_reloading():
    rpc = RPC([snapshot(cards=False), {'content': []}, snapshot()])
    result = discovery_call(rpc, 'browser_snapshot', {})
    assert [call[0] for call in rpc.calls] == ['browser_snapshot', 'browser_wait_for', 'browser_snapshot']
    assert rpc.calls[1][1] == {'text': 'Showing'}
    assert result['_meta']['ddoja']['google_results']['native_job_links'] == 1


def test_observed_public_job_link_uses_full_navigation_in_same_tab():
    rpc = RPC([snapshot(), snapshot(DETAIL, cards=False)])
    result = discovery_call(rpc, 'browser_click', {'target': 'job123', 'element': 'Observed job'})
    assert rpc.calls == [('browser_snapshot', {}), ('browser_navigate', {'url': DETAIL})]
    assert result['_meta']['ddoja_navigation']['actual_tool'] == 'browser_navigate'


def test_back_to_search_uses_exact_observed_filtered_results_url():
    before = snapshot(DETAIL, cards=False)
    before['content'][0]['text'] += '- link "Back to jobs search" [ref=back]:\n  - /url: ' + BOARD + '\n'
    rpc = RPC([before, snapshot()])
    result = discovery_call(rpc, 'browser_click', {'target': 'back'})
    assert rpc.calls[1] == ('browser_navigate', {'url': BOARD})
    assert 'Showing 1 to 20' in text_of(result)


@pytest.mark.parametrize('href', [
    'https://accounts.google.com/ServiceLogin',
    'https://www.google.com/about/careers/applications/jobs/results/123-software-engineer/apply',
    'https://other.invalid/jobs/123',
])
def test_authentication_apply_and_external_links_keep_normal_browser_action(href):
    before = snapshot()
    before['content'][0]['text'] += '- link "Other action" [ref=other]:\n  - /url: ' + href + '\n'
    rpc = RPC([before, {'content': []}])
    discovery_call(rpc, 'browser_click', {'target': 'other'})
    assert rpc.calls[-1] == ('browser_click', {'target': 'other'})


def test_stale_reference_never_turns_into_a_guessed_navigation():
    failed = {'isError': True, 'content': [{'type': 'text', 'text': 'Ref old123 not found'}]}
    rpc = RPC([snapshot(), failed])
    assert discovery_call(rpc, 'browser_click', {'target': 'old123'}) == failed
    assert not any(name == 'browser_navigate' for name, _ in rpc.calls)


def test_permission_error_is_returned_without_another_action():
    denial = {'isError': True, 'content': [{'type': 'text', 'text': 'Browser access denied'}]}
    rpc = RPC([denial])
    assert discovery_call(rpc, 'browser_click', {'target': 'job123'}) == denial
    assert len(rpc.calls) == 1


def test_scoped_snapshot_is_not_expanded_and_wait_failure_is_not_claimed_success():
    rpc = RPC([])
    scoped = snapshot(cards=False)
    assert results_evidence(rpc, scoped, {'target': 'facet', 'depth': 2}) == scoped
    failure = {'isError': True, 'content': [{'type': 'text', 'text': 'Actual results wait timed out'}]}
    rpc = RPC([snapshot(cards=False), failure])
    assert discovery_call(rpc, 'browser_snapshot', {}) == failure


def test_submission_browser_keeps_exact_original_click_without_recovery_observations():
    bridge = BrowserBridge.__new__(BrowserBridge)
    bridge.role = 'submission'; bridge.settings = {'batch_form_edits': False}
    bridge.catalog = [{'name': 'browser_click'}]; bridge.rpc = RPC([{'content': []}])
    bridge.call('browser_click', {'target': 'submit'})
    assert bridge.rpc.calls == [('browser_click', {'target': 'submit'})]


def apple_snapshot(cards=True):
    text='### Page\n- Page URL: https://jobs.apple.com/en-us/search?location=united-states-USA\n### Snapshot\n'
    if cards:
        text+='- link "Software Engineer" [ref=apple123]:\n  - /url: /en-us/details/123/software-engineer\n'
    return {'content':[{'type':'text','text':text}]}


def test_apple_shallow_result_observation_expands_native_cards_without_navigation():
    rpc=RPC([apple_snapshot(False),apple_snapshot()])
    result=discovery_call(rpc,'browser_snapshot',{'depth':3})
    assert rpc.calls==[('browser_snapshot',{'depth':3}),('browser_snapshot',{})]
    assert result['_meta']['ddoja']['apple_results']['native_job_links']==1
    assert 'ref=apple123' in text_of(result)
    assert 'read complete JDs' in text_of(result)


def test_apple_scoped_snapshot_and_permission_failure_are_preserved():
    rpc=RPC([apple_snapshot(False)])
    discovery_call(rpc,'browser_snapshot',{'target':'facet','depth':3})
    assert len(rpc.calls)==1
    denied={'isError':True,'content':[{'type':'text','text':'Access denied'}]}
    rpc=RPC([apple_snapshot(False),denied])
    assert discovery_call(rpc,'browser_snapshot',{'depth':3})==denied


def test_apple_results_already_containing_links_do_not_need_extra_observation():
    rpc=RPC([apple_snapshot()])
    result=discovery_call(rpc,'browser_snapshot',{})
    assert result['_meta']['ddoja']['apple_results']['depth_expanded'] is False
    assert len(rpc.calls)==1


def test_apple_shallow_scoped_results_expand_only_that_observed_results_list():
    shallow=apple_snapshot(False)
    shallow['content'][0]['text']+='- list "Job Opportunities" [ref=results]:\n  - listitem [ref=card]\n'
    rpc=RPC([shallow,apple_snapshot()])
    result=discovery_call(rpc,'browser_snapshot',{'target':'results','depth':4})
    assert rpc.calls[-1]==('browser_snapshot',{'target':'results'})
    assert result['_meta']['ddoja']['apple_results']['native_job_links']==1


def test_discovery_tab_list_preserves_fresh_page_evidence_for_continuation():
    tabs={'content':[{'type':'text','text':'### Result\n- 0: (current) Apple search'}]}
    rpc=RPC([tabs,apple_snapshot()])
    result=discovery_call(rpc,'browser_tabs',{'action':'list'})
    assert rpc.calls==[('browser_tabs',{'action':'list'}),('browser_snapshot',{})]
    assert 'Page URL: https://jobs.apple.com/en-us/search' in text_of(result)
    assert '0: (current) Apple search' in text_of(result)


def test_discovery_tab_permission_error_does_not_trigger_further_observation():
    denied={'isError':True,'content':[{'type':'text','text':'Permission denied'}]}
    rpc=RPC([denied])
    assert discovery_call(rpc,'browser_tabs',{'action':'list'})==denied
    assert len(rpc.calls)==1


def test_tab_list_does_not_mask_a_results_observation_failure():
    failed={'isError':True,'content':[{'type':'text','text':'Results did not load'}]}
    rpc=RPC([{'content':[]},snapshot(cards=False),failed])
    result=discovery_call(rpc,'browser_tabs',{'action':'list'})
    assert result['isError'] is True
    assert 'Results did not load' in text_of(result)
