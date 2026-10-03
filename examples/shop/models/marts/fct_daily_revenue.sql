select
    ordered_at as order_date,
    currency,
    count(*) as orders,
    sum(amount) as revenue
from {{ ref('stg_orders') }}
where status not in ('cancelled', 'returned')
group by 1, 2
