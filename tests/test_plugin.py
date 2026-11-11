import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from crown.plugin import Runtime, register


class Context:
    def __init__(self, path):
        self.config = {'database': str(path), 'instance_name': 'research'}
        self.tools, self.hooks, self.commands, self.cli, self.cleanups = {}, {}, {}, {}, []
    def get_config(self, key, default=None):
        return self.config.get(key, default)
    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs
    def register_hook(self, name, handler):
        self.hooks[name] = handler
    def register_command(self, name, **kwargs):
        self.commands[name] = kwargs
    def register_cli_command(self, **kwargs):
        self.cli[kwargs['name']] = kwargs
    def on_unload(self, callback):
        self.cleanups.append(callback)


class PluginTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'record.db'
        self.ctx = Context(self.path)
        self.runtime = Runtime(self.ctx)
    def tearDown(self):
        self.runtime.stop()
        for callback in self.ctx.cleanups:
            callback()
        self.temp.cleanup()

    def test_loading_has_no_data_or_process_side_effects(self):
        register(self.ctx)
        self.assertFalse(self.path.exists())
        self.assertEqual(set(self.ctx.tools), {'crown_context', 'crown_report'})
        self.assertNotIn('crown_answer', self.ctx.tools)

    def test_local_answer_reaches_another_instance_context(self):
        args = {'event_id': 'q1', 'task_id': 'job', 'kind': 'question', 'text': 'Which format?'}
        result = json.loads(self.runtime.report(args, session_id='segment', conversation_id='conversation'))
        self.assertTrue(result['ok'])
        self.runtime.command('answer q1 Markdown', platform='local')
        other_ctx = Context(self.path)
        other_ctx.config['instance_name'] = 'writer'
        other = Runtime(other_ctx)
        try:
            state = json.loads(other.context({'task_id': 'job'}))
            self.assertEqual(state['answers'][0]['text'], 'Markdown')
            self.assertEqual(state['answers'][0]['session_id'], 'conversation')
            self.assertEqual(state['answers'][0]['source'], 'research')
            self.assertEqual(state['pending'], [])
            self.assertIn('Markdown', other.recall()['context'])
        finally:
            other.stop()

    def test_remote_or_missing_surface_cannot_enroll_or_answer(self):
        for platform in ('telegram', 'discord', ''):
            self.assertIn('local-operator only', self.runtime.command('here', platform=platform))
        self.assertFalse(self.path.exists())

    def test_report_error_is_structured_and_does_not_claim_delivery(self):
        result = json.loads(self.runtime.report({'event_id': 'missing-fields'}))
        self.assertFalse(result['ok'])
        self.assertNotIn('delivered', result)

    def test_native_directory_entry_imports_and_registers(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('crown_test_native', root / '__init__.py',
                                                      submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.register(self.ctx)
        parser = argparse.ArgumentParser()
        self.ctx.cli['crown']['setup_fn'](parser)
        args = parser.parse_args(['--db', str(self.path), 'status'])
        self.assertEqual(args.crown_command, 'status')


if __name__ == '__main__':
    unittest.main()
