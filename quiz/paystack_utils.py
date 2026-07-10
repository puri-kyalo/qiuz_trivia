# quiz/paystack_utils.py
import hmac
import hashlib
import requests
import logging
from decimal import Decimal
from django.conf import settings

logger = logging.getLogger(__name__)

def verify_paystack_signature(payload_body, signature_header):
    """
    Validates incoming Webhook signatures using an HMAC SHA512 hash
    against PAYSTACK_SECRET_KEY to ensure authenticity.
    """
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', '')
    if not secret_key:
        logger.error("PAYSTACK_SECRET_KEY configuration is missing.")
        return False
        
    computed_signature = hmac.new(
        secret_key.encode('utf-8'),
        payload_body,
        hashlib.sha512
    ).hexdigest()
    
    return hmac.compare_digest(computed_signature, signature_header)

def verify_paystack_transaction(reference):
    """
    Queries Paystack's REST verification endpoint in real-time.
    Converts minor currency subunits (cents) to standard Decimal KES values.
    Calculates the clean net amount by deducting transaction fees.
    """
    secret_key = getattr(settings, 'PAYSTACK_SECRET_KEY', None)
    if not secret_key:
        logger.error("PAYSTACK_SECRET_KEY is missing from configuration variables.")
        return None

    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            response_data = response.json()
            if response_data.get('status') and response_data.get('data', {}).get('status') == 'success':
                data = response_data['data']
                
                # Convert total amount from minor units (cents) to standard KES
                amount_minor = Decimal(str(data.get('amount', 0)))
                amount_kes = amount_minor / Decimal('100.00')
                
                # Extract and convert the gateway processing fee
                fee_minor = Decimal(str(data.get('fees', 0)))
                fee_kes = fee_minor / Decimal('100.00')
                
                return {
                    'status': 'SUCCESS',
                    'amount': amount_kes,
                    'fee': fee_kes,                      # Paystack's structural transaction fee
                    'net_amount': amount_kes - fee_kes,  # Total clean funds remaining
                    'email': data.get('customer', {}).get('email'),
                    'metadata': data.get('metadata'),
                }
        return {'status': 'FAILED', 'amount': Decimal('0.00')}
    except requests.RequestException as e:
        logger.error(f"Paystack API network drop encountered for reference {reference}: {str(e)}")
        return None