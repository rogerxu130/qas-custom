from qas_custom.modules.billing.invoice_corrections import repair_trial_amendment_links


def execute():
    result = repair_trial_amendment_links(dry_run=False)
    print("Trial amendment link repair:", result)
