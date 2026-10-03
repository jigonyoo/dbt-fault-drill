{% test row_count_at_least(model, min_rows) %}
select count(*) as row_count
from {{ model }}
having count(*) < {{ min_rows }}
{% endtest %}
