from qas_custom.modules.billing.trial_invoice_dates import repair_trial_invoice_dates


def execute():
    repair_trial_invoice_dates(dry_run=False)
