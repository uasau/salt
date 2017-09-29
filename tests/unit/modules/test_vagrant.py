# -*- coding: utf-8 -*-

# Import python libs
from __future__ import absolute_import

import os
# Import Salt Testing libs
from tests.support.mixins import LoaderModuleMockMixin
from tests.support.unit import skipIf, TestCase
from tests.support.mock import NO_MOCK, NO_MOCK_REASON

# Import salt libs
import salt.modules.vagrant as vagrant
import salt.modules.cmdmod as cmd
import salt.utils.sdb as sdb
import salt.exceptions

# Import third party libs
from salt.ext import six

TEMP_DATABASE_FILE = '/tmp/salt-tests-tmpdir/test_vagrant.sqlite'


@skipIf(NO_MOCK, NO_MOCK_REASON)
class VagrantTestCase(TestCase, LoaderModuleMockMixin):
    '''
    Unit TestCase for the salt.modules.vagrant module.
    '''
    @classmethod
    def tearDownClass(cls):
        try:
            os.unlink(TEMP_DATABASE_FILE)
        except OSError:
            pass

    def setup_loader_modules(self):
        vagrant_globals = {
            '__opts__': {
                'extension_modules': '',
                'vagrant_sdb_data': {
                    'driver': 'sqlite3',
                    'database': TEMP_DATABASE_FILE,
                    'table': 'sdb',
                    'create_table': True
                }
            },
            '__salt__': {
                'cmd.shell': cmd.shell,
                'cmd.retcode': cmd.retcode,
            },
            '__utils__': {
                'sdb.sdb_set': sdb.sdb_set,
                'sdb.sdb_get': sdb.sdb_get,
                'sdb.sdb_delete': sdb.sdb_delete
            }
        }
        return {vagrant: vagrant_globals}

    def test_vagrant_get_vm_info(self):
        with self.assertRaises(salt.exceptions.SaltInvocationError):
            vagrant.get_vm_info('thisNameDoesNotExist')

    def test_vagrant_init_positional(self):
        resp = vagrant.init(
            'test1',
            '/tmp/nowhere',
            'onetest',
            'nobody',
            False,
            'french',
            {'different': 'very'}
            )
        self.assertIsInstance(resp, six.string_types)
        resp = vagrant.get_vm_info('test1')
        expected = dict(name='test1',
                        cwd='/tmp/nowhere',
                        machine='onetest',
                        runas='nobody',
                        vagrant_provider='french',
                        different='very'
                        )
        self.assertEqual(resp, expected)

    def test_vagrant_init_dict(self):
        testdict = dict(cwd='/tmp/anywhere',
                        machine='twotest',
                        runas='somebody',
                        vagrant_provider='english')
        vagrant.init('test2', vm=testdict)
        resp = vagrant.get_vm_info('test2')
        testdict['name'] = 'test2'
        self.assertEqual(resp, testdict)

    def test_vagrant_init_arg_override(self):
        testdict = dict(cwd='/tmp/there',
                        machine='treetest',
                        runas='anybody',
                        vagrant_provider='spansh')
        vagrant.init('test3',
                        cwd='/tmp',
                        machine='threetest',
                        runas='him',
                        vagrant_provider='polish',
                        vm=testdict)
        resp = vagrant.get_vm_info('test3')
        expected = dict(name='test3',
                        cwd='/tmp',
                        machine='threetest',
                        runas='him',
                        vagrant_provider='polish')
        self.assertEqual(resp, expected)

    def test_vagrant_get_ssh_config_fails(self):
        vagrant.init('test3', cwd='/tmp')
        with self.assertRaises(salt.exceptions.CommandExecutionError):
            vagrant.get_ssh_config('test3')  # has not been started

    def test_vagrant_destroy_removes_cached_entry(self):
        vagrant.init('test3', cwd='/tmp')
        #  VM has a stored value
        self.assertEqual(vagrant.get_vm_info('test3')['name'], 'test3')
        #  clean up (an error is expected -- machine never started)
        self.assertFalse(vagrant.destroy('test3'))
        #  VM no longer exists
        with self.assertRaises(salt.exceptions.SaltInvocationError):
            vagrant.get_ssh_config('test3')
