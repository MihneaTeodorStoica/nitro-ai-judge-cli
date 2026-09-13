#!/usr/bin/env python3
"""Capture real UI pixels from entirely fictional, offline fixtures.

Developer dependencies: pip install pillow cairosvg playwright
Then: python -m playwright install chromium; python scripts/capture-assets.py
Does not contact Nitro, Docker, a registry, or any public service. Not an E2E
runtime recording: the release MP4 still requires a real local notebook demo.
"""
from __future__ import annotations
import asyncio
import contextlib
import io
import os
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT))
from PIL import Image
import cairosvg
from playwright.async_api import async_playwright
from aiohttp.test_utils import TestServer
from nitro_ai_judge_cli import state, tui
from nitro_ai_judge_cli.manager import app as manager_app
from nitro_ai_judge_cli.manager.store import ManagerStore
from tests.test_manager import FakeBackend
from tests.test_tui import FakeManager

ASSETS = ROOT/'docs/assets'
CONTESTS = [{'organizationSlug':'demo-lab','competitionSlug':slug,'title':title} for slug,title in [('spring-challenge','Spring Challenge'),('vision-lab','Vision Lab'),('signal-sprint','Signal Sprint')]]
TASKS = [{'id':'demo-task-1','title':'Forest Forecast','synopsis':'Predict tomorrow. Learn from yesterday.',
          'statement':'# Forest Forecast\n\nBuild a model that predicts the next day’s canopy temperature.\n\n## Your dataset\n\n- Daily weather observations\n- Soil moisture and canopy measurements\n- All examples in this capture are fictional\n\n## Evaluation\n\nSubmissions are scored using mean absolute error. Lower is better.\n\n## Getting started\n\nDownload the training data, open a notebook with Play, and submit your predictions.'},
         {'id':'demo-task-2','title':'River Signals','synopsis':'Find patterns in a fictional sensor network.'}]

async def capture_tui():
    state.update_cache('contests','all',CONTESTS)
    state.set_contest(CONTESTS[0])
    state.update_cache('tasks','demo-lab/spring-challenge',TASKS)
    state.set_task(TASKS[0])
    with contextlib.ExitStack() as stack:
        for name, value in {
            'load_state':{'access_token':'offline-fixture','refresh_token':'offline-fixture'},
            'ensure_fresh_state':{'access_token':'offline-fixture','refresh_token':'offline-fixture'},
            'get_auth':('','','offline-fixture'), 'load_competitions':CONTESTS,
            'load_tasks':TASKS, 'load_task_view':{'task':TASKS[0]},
            'load_task_file_categories':['statement','train_data','test_data','sample_output'],
            'load_submissions':([],1),
        }.items(): stack.enter_context(patch.object(tui,name,return_value=value))
        app=tui.NitroTUI(manager_client=FakeManager())
        async with app.run_test(size=(120,34)) as pilot:
            await pilot.pause(.3)
            app.active_pane='right'; app.action_view(1)
            app.set_status('Fictional offline demo · Tab panes · 1–4 views · ? help')
            await pilot.pause()
            def frame():
                return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=app.export_screenshot(title='NAIJ 3.2 · fictional demo').encode()))).convert('RGB')
            first=frame()
            first.save(ASSETS/'tui-overview.png', optimize=True)
            frames=[first]
            for keys in [('tab',),('tab',),('2',),('3',),('1',),('question_mark',),('escape',)]:
                await pilot.press(*keys); await pilot.pause(.15); frames.append(frame())
            frames[0].save(ASSETS/'tui-keyboard.gif',save_all=True,append_images=frames[1:],duration=1250,loop=0,optimize=True)

class SeededBackend(FakeBackend):
    async def discover(self):
        return [await self.inspect_competition('demo-lab',item['competitionSlug']) for item in CONTESTS]
    async def inspect_competition(self, org, competition):
        item=await super().inspect_competition(org,competition)
        index=next(i for i,c in enumerate(CONTESTS) if c['competitionSlug']==competition)
        item.update(title=CONTESTS[index]['title'],workspace_state=['running','stopped','missing'][index],service_health=['healthy','stopped','unknown'][index],image_state='missing' if index==2 else 'ready',containers=0 if index==2 else 2)
        if index==2: item['images']={key:{**value,'state':'missing'} for key,value in item['images'].items()}
        return item

async def capture_manager(root):
    store=ManagerStore(str(root/'manager.db'))
    app=manager_app.create_app(store=store,backend=SeededBackend(),api_token=secrets.token_hex(32))
    with patch.object(store,'credentials',return_value={'fixture_connected':True}), patch.object(manager_app,'_remote_competitions',new=AsyncMock(return_value=([],False))):
        server=TestServer(app)
        await server.start_server()
        origin=str(server.make_url('/')).rstrip('/')
        app['allowed_hosts'].add(origin.split('://')[1])
        app['public_origin']=origin
        try:
            async with async_playwright() as pw:
                browser=await pw.chromium.launch()
                page=await browser.new_page(viewport={'width':1440,'height':900},device_scale_factor=1)
                # A failed fixture must never silently contact a live service.
                await page.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
                await page.goto(origin+'/nitro/')
                await page.wait_for_selector('.competition-row')
                await page.wait_for_function("document.querySelectorAll('.competition-row').length===3")
                await page.evaluate('document.fonts.ready')
                await page.screenshot(path=str(ASSETS/'play-manager.png'))
                await browser.close()
        finally: await server.close()
    with Image.open(ASSETS/'play-manager.png') as image: image.save(ASSETS/'play-manager.png',optimize=True)

async def main():
    ASSETS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'NAIJ_STATE_DIR':str(Path(directory)/'state')}):
        state.configure_state_dir(None); state.reset_state_paths()
        try:
            await capture_tui()
            await capture_manager(Path(directory))
        finally: state.reset_state_paths()
    for path in sorted(ASSETS.glob('*')):
        if path.suffix in {'.png','.gif'}: print(f'{path.relative_to(ROOT)}: {path.stat().st_size:,} bytes')

if __name__=='__main__': asyncio.run(main())
