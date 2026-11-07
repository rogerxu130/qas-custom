"""Site-free teaching resource XML import and regression suite. Never uses a live site."""
import sys, types, unittest
from pathlib import Path
sys.dont_write_bytecode = True
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
frappe = types.ModuleType('frappe')
class Dict(dict):
    def __getattr__(self, key): return self.get(key)
frappe._dict = Dict
utils = types.ModuleType('frappe.utils')
utils.cint = lambda value: int(value or 0)
utils.get_datetime = lambda value: value
utils.now_datetime = datetime.now
utils.add_days = lambda value, days: value + timedelta(days=days)
sys.modules['frappe'] = frappe
sys.modules['frappe.utils'] = utils
names = ['qas_custom.tests.' + name for name in ['test_teaching_resource_import','test_teaching_resources','test_teaching_resource_rules','test_teaching_resource_tags','test_teaching_resource_privacy','test_teaching_resource_previews']]
result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(names))
sys.exit(not result.wasSuccessful())
