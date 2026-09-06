from __future__ import annotations
import asyncio
import threading
import unittest
from unittest.mock import patch
from textual.widgets import Input, Static, Button
from tests import test_tui as fixtures
from nitro_ai_judge_cli import tui


class TUIRequirements(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.TUIPilotTests.setUp
    tearDown = fixtures.TUIPilotTests.tearDown
    auth_patches = fixtures.TUIPilotTests.auth_patches
    cache_selection = fixtures.TUIPilotTests.cache_selection

    async def test_search_next_previous_no_matches_escape_and_compact(self):
        self.cache_selection()
        for size in ((120,34), (70,20)):
            with self.auth_patches(tasks=fixtures.TASKS):
                app = tui.NitroTUI(manager_client=fixtures.FakeManager())
                async with app.run_test(size=size) as pilot:
                    await pilot.pause(.2)
                    app.current_task = {**fixtures.TASKS[0], 'statement':'Needle first\n' + 'plain line\n'*60 + 'NEEDLE last'}
                    app.action_view(1)
                    with patch.object(tui, 'load_task_view', side_effect=AssertionError('search must be local')):
                        app.action_filter()
                        field = app.query_one('#overview-filter', Input)
                        field.value = 'needle'
                        await pilot.pause()
                        self.assertEqual(len(app.overview_matches), 2)
                        await pilot.press('enter','f3')
                        await pilot.pause()
                        self.assertEqual(app.overview_match_index, 1)
                        self.assertGreater(app.query_one('#view-overview').scroll_y, 20)
                        await pilot.press('shift+f3')
                        await pilot.pause()
                        self.assertEqual(app.overview_match_index, 0)
                        field.value = 'not present'
                        await pilot.pause()
                        self.assertIn('0/0', str(app.query_one('#status-line').content))
                        await pilot.press('escape')
                        await pilot.pause()
                        self.assertFalse(field.has_class('-open'))
                        self.assertTrue(app.query_one('#overview').display)
                        self.assertEqual(app.focused.id, 'view-overview')

    async def test_follow_pause_resume_bounded_history_and_exit(self):
        self.cache_selection()
        class Manager(fixtures.FakeManager):
            def __init__(self): super().__init__(); self.closed = asyncio.Event(); self.loaded = asyncio.Event(); self.calls = 0
            async def async_follow_logs(self, *reference):
                self.calls += 1
                try:
                    for i in range(2050):
                        yield f'line {i}'
                        if i % 100 == 0: await asyncio.sleep(0)
                    self.loaded.set()
                    await asyncio.Event().wait()
                finally: self.closed.set()
        manager = Manager()
        with self.auth_patches(tasks=fixtures.TASKS):
            app = tui.NitroTUI(manager_client=manager)
            async with app.run_test(size=(120,34)) as pilot:
                await pilot.pause(.2)
                app.action_view(4)
                app.action_toggle_logs()
                await asyncio.wait_for(manager.loaded.wait(), 4)
                await pilot.pause()
                self.assertEqual(len(app.log_lines), 2000)
                self.assertEqual(app.log_lines[-1], 'line 2049')
                app.action_toggle_logs()
                await asyncio.wait_for(manager.closed.wait(), 2)
                self.assertFalse(app.logs_following)
                self.assertEqual(len(app.log_lines), 2000)
                manager.closed.clear()
                app.action_toggle_logs()
                await pilot.pause(.1)
                self.assertEqual(manager.calls, 2)
                app.action_view(1)
                await asyncio.wait_for(manager.closed.wait(), 2)
                self.assertFalse(app.logs_following)

    async def test_log_disconnect_retains_lines_and_retry_is_available(self):
        self.cache_selection()
        class Manager(fixtures.FakeManager):
            async def async_follow_logs(self, *reference):
                yield 'retained log'
                raise RuntimeError('connection lost')
        with self.auth_patches(tasks=fixtures.TASKS):
            app = tui.NitroTUI(manager_client=Manager())
            async with app.run_test(size=(120,34)) as pilot:
                await pilot.pause(.2)
                app.action_view(4)
                await pilot.pause(.1)
                app.action_toggle_logs()
                await pilot.pause(.2)
                self.assertEqual(list(app.log_lines), ['retained log'])
                self.assertIn('retries', str(app.query_one('#status-line').content))
                self.assertFalse(app.logs_following)

    async def test_operation_progress_and_exact_cancel_race(self):
        self.cache_selection()
        class Manager(fixtures.FakeManager):
            def __init__(self):
                super().__init__(); self.started=threading.Event(); self.release=threading.Event(); self.cancelled=[]
                self.record={'id':'exact-operation','action':'play','status':'running','stage':'pulling','message':'Pulling image'}
            def action(self,*args,**kwargs): return {'operation_id':'exact-operation'}
            def wait_operation(self, operation_id, *, progress=None, **kwargs):
                if progress: progress({'sequence':1,'stage':'pulling','message':'Pulling image'})
                self.started.set(); self.release.wait(3)
                return {**self.record, 'status':'complete'}
            def cancel(self, operation_id):
                self.cancelled.append(operation_id)
                self.record['status']='complete'  # completed before cancellation reached manager
                self.release.set()
                return self.record
        manager=Manager()
        with self.auth_patches(tasks=fixtures.TASKS):
            app=tui.NitroTUI(manager_client=manager)
            async with app.run_test(size=(120,34)) as pilot:
                await pilot.pause(.2)
                operation=asyncio.create_task(app.perform_play_action('play'))
                await asyncio.to_thread(manager.started.wait, 2)
                await pilot.pause(.1)
                self.assertIn('Pulling image', str(app.query_one('#play-operation').content))
                self.assertFalse(app.query_one('#play-cancel',Button).disabled)
                app.action_cancel_play()
                await operation
                await pilot.pause(.2)
                self.assertEqual(manager.cancelled,['exact-operation'])
                self.assertIn('complete', str(app.query_one('#play-operation').content))
                self.assertTrue(app.query_one('#play-cancel',Button).disabled)

    async def test_footer_context_help_and_small_modals(self):
        self.cache_selection()
        with self.auth_patches(tasks=fixtures.TASKS):
            app=tui.NitroTUI(manager_client=fixtures.FakeManager())
            async with app.run_test(size=(70,20)) as pilot:
                await pilot.pause(.2)
                for pane, view in [('contests',1),('tasks',1),('right',1),('right',2),('right',3),('right',4)]:
                    app.active_pane=pane; app.active_view=view
                    app._refresh_context_bindings()
                    shown=[b for group in app._bindings.key_to_bindings.values() for b in group if b.show]
                    self.assertLessEqual(len(shown),5)
                    app.action_help()
                    await pilot.pause()
                    self.assertIsInstance(app.screen,tui.HelpScreen)
                    self.assertTrue(app.screen.context)
                    await pilot.press('escape')
                app.push_screen(tui.DownloadScreen(['statement','train_data','test_data','custom_archive']))
                await pilot.pause()
                app.screen.query_one('#download-output',Input).focus()
                await pilot.pause()
                self.assertGreater(app.screen.query_one('#download-dialog').max_scroll_y,0)
                await pilot.press('escape')

    async def test_path_suggestion_acceptance_keeps_tab_for_focus(self):
        self.cache_selection()
        (self.root/'answer.csv').write_text('fixture')
        with self.auth_patches(tasks=fixtures.TASKS):
            app=tui.NitroTUI(manager_client=fixtures.FakeManager())
            async with app.run_test(size=(110,30)) as pilot:
                await pilot.pause(.2)
                app.push_screen(tui.SubmitScreen())
                await pilot.pause()
                field=app.screen.query_one('#submit-output',Input)
                field.value=str(self.root/'ans'); field.cursor_position=len(field.value)
                await pilot.pause(.2)
                await pilot.press('right')
                self.assertEqual(field.value,str(self.root/'answer.csv'))
                await pilot.press('tab')
                self.assertEqual(app.focused.id,'submit-source')
                await pilot.press('escape')
