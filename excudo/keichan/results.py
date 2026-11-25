"""Bound factual receipt details returned to the conversation model."""
import json


def receipt_result(entry):
    record = entry.get('record') or {}
    fields = ('date', 'merchant', 'total_amount', 'currency', 'category',
              'payment_method', 'summary', 'category_needs_review', 'entered_by')
    result = {'name': entry['name'], 'status': entry['status'],
              'record': {key: record[key] for key in fields if key in record}}
    if entry.get('problems'):
        result['problems'] = list(entry['problems'])
    return result


class ReceiptResults:
    """Keep complete entries within the context budget; count omitted details."""
    def __init__(self, *, max_chars=8000):
        self.items = []
        self.omitted = 0
        self._size = 0
        self._max_chars = max_chars

    def add(self, entry, *, include_record=True):
        details = receipt_result(entry)
        if not include_record:
            details.pop('record')
        size = len(json.dumps(details, ensure_ascii=False))
        if len(self.items) < 20 and self._size + size <= self._max_chars:
            self.items.append(details)
            self._size += size
        else:
            self.omitted += 1
