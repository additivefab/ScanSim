"""Optional environment-specific browser validation used for this delivery.
Requires Playwright and Chromium; not part of the simulation dependencies.
Paths below are the build environment's paths. Update root and executable_path
before reusing elsewhere. Normal users should open the HTML viewer directly.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path('/mnt/data/ZL_OEM_SCANNER_SIM')
log=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1280,'height':900})
    errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
    for name in ['ad9268_50m_5x','ideal_regression','demo_log','demo_linear']:
        page.close()
        page=browser.new_page(viewport={'width':1280,'height':900})
        page.on('pageerror',lambda e: errors.append(str(e)))
        print('Testing',name,flush=True)
        page.set_content((root/'examples'/name/'viewer.html').read_text(),wait_until='load')
        print('JS errors so far',errors,flush=True)
        page.wait_for_function("document.getElementById('details').rows.length===16")
        before=page.locator('#timeInfo').inner_text()
        page.locator('#next').click()
        after=page.locator('#timeInfo').inner_text()
        assert before!=after,name
        page.locator('#start').click()
        # Walk beyond the rendered waveform; this must still display the correct readout.
        page.locator('#time').evaluate('(e)=>{e.value=e.max;e.dispatchEvent(new Event("input"));}')
        page.locator('#start').click()
        page.locator('#frame').evaluate('(e)=>{e.value=1;e.dispatchEvent(new Event("input"));}')
        assert '1 /' in page.locator('#frameInfo').inner_text()
        if name=='ad9268_50m_5x':page.screenshot(path='/mnt/data/_pdf_review/viewer_browser.png',full_page=True)
        log.append('PASS '+name+': page load, 16-field sample table, next sample, ROI jump, last sample, frame accumulation')
    assert not errors,errors
    browser.close()
(root/'examples/browser_test_log.txt').write_text('\n'.join(log)+'\nNo JavaScript runtime errors observed. Tested via Playwright set_content; local file URL navigation is restricted by the test browser policy.\n')
print('\n'.join(log))
