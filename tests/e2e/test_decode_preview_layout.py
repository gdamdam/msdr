"""Latest decoder messages stay within the preview without resizing its panel."""
import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize('width', [280, 420, 700])
@pytest.mark.parametrize('sidebar', [False, True])
def test_preview_truncates_without_resizing(live_app, page, width, sidebar):
    page.goto(live_app.base_url)
    page.wait_for_function("() => customElements.get('decode-deck')")
    result = page.evaluate("""async ({width, sidebar}) => {
      document.querySelector('msdr-app').remove();
      const host = document.createElement('div');
      host.className = sidebar ? 'survey-first-sidebar' : '';
      host.style.width = `${width}px`;
      host.innerHTML = '<div class="listen-decode-controls"><decode-deck></decode-deck></div>';
      document.body.append(host);
      const deck = host.querySelector('decode-deck');
      deck.status = {active: true, decoders: [{id:'ais', state:'running'}]};
      const event = {ts:1791055080, decoder:'ais', kind:'text', integrity:'passed',
        text:'AIS type 1'};
      deck.lastEvent = event;
      await deck.updateComplete;
      const panel = host.querySelector('.listen-decode-controls');
      const before = panel.getBoundingClientRect();
      const full = 'AIS type 1 MMSI 367380880 37.77142,-122.30010 SOG 0.0 kn ' +
        'COG 358.8 under way using engine ' + 'X'.repeat(400);
      deck.lastEvent = {...event, text:full};
      await deck.updateComplete;
      const after = panel.getBoundingClientRect();
      const text = host.querySelector('.decode-last-event-text');
      const badge = host.querySelector('.decode-last-event-line .integrity-badge');
      const row = text.parentElement.getBoundingClientRect();
      const br = badge.getBoundingClientRect();
      return {heightChange:after.height-before.height, widthChange:after.width-before.width,
        clipped:text.scrollWidth>text.clientWidth, nowrap:getComputedStyle(text).whiteSpace,
        ellipsis:getComputedStyle(text).textOverflow, preserved:text.textContent.includes(full),
        tooltip:text.title.includes(full), badgeVisible:br.left>=row.left && br.right<=row.right+1};
    }""", {'width': width, 'sidebar': sidebar})
    assert abs(result['heightChange']) < 1
    assert abs(result['widthChange']) < 1
    assert result['clipped'] and result['preserved'] and result['tooltip']
    assert result['nowrap'] == 'nowrap' and result['ellipsis'] == 'ellipsis'
    assert result['badgeVisible']
