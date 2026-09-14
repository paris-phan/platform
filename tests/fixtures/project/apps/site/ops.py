"""Filesystem deployment fixture for release lifecycle tests."""
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    operation = sys.argv[1]
    source = Path(__file__).with_name('index.html')
    if operation == 'check':
        assert b'<title>Platform reference</title>' in source.read_bytes()
        return
    bundle = Path(os.environ['PLATFORM_BUNDLE_DIR'])
    if operation == 'build':
        shutil.copyfile(source, bundle / 'index.html')
        (bundle / 'release.txt').write_text(os.environ['PLATFORM_RELEASE'])
        return
    state = Path(os.environ['PLATFORM_EXAMPLE_STATE']) / os.environ['PLATFORM_ENVIRONMENT']
    state.mkdir(parents=True, exist_ok=True)
    if operation == 'deploy':
        for path in bundle.iterdir():
            with tempfile.NamedTemporaryFile(dir=state, delete=False) as temporary:
                temporary.write(path.read_bytes())
                name = temporary.name
            os.replace(name, state / path.name)
    elif operation == 'verify':
        for path in bundle.iterdir():
            actual = state / path.name
            assert hashlib.sha256(actual.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest()
        assert (state / 'release.txt').read_text() == os.environ['PLATFORM_RELEASE']
    else:
        raise ValueError('unsupported command: ' + operation)


if __name__ == '__main__':
    main()
