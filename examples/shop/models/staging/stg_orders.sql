select
    order_id,
    customer_id,
    ordered_at,
    status,
    currency,
    amount
from {{ ref('orders') }}
