import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

class DeliveryRetryTests(unittest.TestCase):
    def test_telegram_rate_limit_honors_retry_after(self):
        module=load('send_telegram_notification')
        response=MagicMock();response.__enter__.return_value.read.return_value=b'{"ok":true,"result":{"message_id":1}}'
        error=HTTPError('https://example.invalid',429,'rate limited',{},io.BytesIO(b'{"parameters":{"retry_after":2}}'))
        with patch.object(module,'_load_credentials',return_value=('fixture','fixture')),patch.object(module.urllib.request,'urlopen',side_effect=[error,response]) as request,patch.object(module.time,'sleep') as sleep:
            result=module._telegram_request('sendMessage',{'text':'fixture'})
        self.assertTrue(result['ok']);self.assertEqual(request.call_count,2);sleep.assert_called_once_with(3)

    def test_completed_parts_are_not_resent(self):
        module=load('run_revised_research');db=sqlite3.connect(':memory:')
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'card.txt';file.write_text('RESEARCH DETAILS | NFL\n'+'field\n'*1000)
            with patch.object(module.subprocess,'run') as send,patch.object(module.time,'sleep'):
                module.send_card(file,db,'fixture');first=send.call_count
                module.send_card(file,db,'fixture')
            self.assertGreater(first,1);self.assertEqual(send.call_count,first)
