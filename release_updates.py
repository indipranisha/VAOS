"""Bounded, cached checks of published stable releases; never installs code."""
from __future__ import annotations
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPOSITORY = 'https://github.com/indipranisha/VAOS'
API_URL = 'https://api.github.com/repos/indipranisha/VAOS/releases/latest'
_lock = threading.Lock()
_cached = None
_checked = 0.0


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('Expected stable semantic version')
    return tuple(int(part) for part in value.lstrip('v').split('.'))


def _newer(latest, installed):
    def key(value):
        match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-beta\.?(\d+))?',value or '')
        if not match:raise ValueError('Unsupported version')
        major,minor,patch,beta=match.groups()
        return (int(major),int(minor),int(patch),1 if beta is None else 0,int(beta or 0))
    try:return key(latest)>key(installed)
    except (ValueError,TypeError):return False


def check_release(installed):
    """Cache successes and failures for one hour; send no credentials or local data."""
    global _cached, _checked
    if os.getenv('VAOS_UPDATE_CHECK', '1').lower() in ('0', 'false', 'off'):
        return {'status': 'disabled', 'installed': installed}
    with _lock:
        if _cached is not None and time.monotonic() - _checked < 3600:
            return {**_cached, 'installed': installed, 'update_available': bool(_cached.get('latest') and _newer(_cached['latest'],installed))}
        result = {'status': 'unavailable', 'update_available': False,
                  'releases_url': REPOSITORY + '/releases',
                  'checked_at': datetime.now(timezone.utc).isoformat()}
        request = urllib.request.Request(API_URL, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'VAOS-release-check'})
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                raw = response.read(512001)
            if len(raw) > 512000:
                raise ValueError('Release response too large')
            release = json.loads(raw)
            latest = release.get('tag_name')
            version_tuple(latest)
            if release.get('draft') or release.get('prerelease'):
                raise ValueError('Not a stable release')
            # Build our own link: untrusted response URLs never become UI links.
            result.update(status='ok', latest=latest.lstrip('v'),
                          release_url=REPOSITORY + '/releases/tag/' + latest,
                          update_available=_newer(latest,installed))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                result['status'] = 'no_release'
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        _cached, _checked = result, time.monotonic()
        return {**result, 'installed': installed}
