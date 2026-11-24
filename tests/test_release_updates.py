import io
import json
import unittest
import urllib.error
from unittest.mock import patch
import release_updates as updates

class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        updates._cached = None
        updates._checked = 0
        self.env = patch.dict('os.environ', {'VAOS_UPDATE_CHECK':'1'})
        self.env.start()
    def tearDown(self):
        self.env.stop()
    def response(self, tag='v1.3.0', **extra):
        return io.BytesIO(json.dumps(dict(tag_name=tag, draft=False, prerelease=False, **extra)).encode())
    def test_numeric_comparison_and_cache(self):
        with patch.object(updates.urllib.request,'urlopen',return_value=self.response('v1.2.10')) as request:
            self.assertTrue(updates.check_release('1.2.9')['update_available'])
            self.assertFalse(updates.check_release('1.2.11')['update_available'])
            request.assert_called_once()
            self.assertEqual(request.call_args.kwargs['timeout'],4)
    def test_untrusted_url_not_forwarded(self):
        with patch.object(updates.urllib.request,'urlopen',return_value=self.response(html_url='javascript:alert(1)')):
            result=updates.check_release('1.2.5')
        self.assertTrue(result['release_url'].startswith(updates.REPOSITORY+'/releases/tag/'))
    def test_invalid_and_prerelease_not_offered(self):
        for payload in [dict(tag_name='v9.0.0',prerelease=True),dict(tag_name='v2.0.0-rc1'),dict(tag_name='../../x'),None]:
            updates._cached=None
            with patch.object(updates.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
                self.assertFalse(updates.check_release('1.2.5')['update_available'])
    def test_offline_and_no_releases(self):
        for error,expected in [(OSError('offline'),'unavailable'),(urllib.error.HTTPError(updates.API_URL,404,'missing',{},None),'no_release')]:
            updates._cached=None
            with patch.object(updates.urllib.request,'urlopen',side_effect=error) as request:
                self.assertEqual(updates.check_release('1.2.5')['status'],expected)
                updates.check_release('1.2.5')
                request.assert_called_once()
    def test_opt_out(self):
        with patch.dict('os.environ',{'VAOS_UPDATE_CHECK':'0'}),patch.object(updates.urllib.request,'urlopen') as request:
            self.assertEqual(updates.check_release('1.2.5')['status'],'disabled')
            request.assert_not_called()
