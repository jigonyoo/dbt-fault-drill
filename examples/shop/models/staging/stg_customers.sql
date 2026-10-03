select
    customer_id,
    country
from {{ ref('customers') }}
