with stock_info as (
  SELECT 
    sd.stock_pk,
    sd.company_name,
    sd.ticker,
    d.date_pk,
    d.full_date,
    smf.close_price,
    row_number() over(partition by sd.stock_pk order by d.full_date DESC) as rn
  FROM
    `{{dataset}}.stock_metric_fct` as smf
    inner join `{{dataset}}.stock_dim` as sd
    on smf.stock_fk = sd.stock_pk
    inner join `{{dataset}}.date_dim` as d
    on smf.date_fk = d.date_pk)

, most_recent as (
  SELECT
    *
    EXCEPT(rn)
  FROM
    stock_info
  WHERE
    rn = 1
)

, second_most_recent as (
    SELECT
    *
    EXCEPT(rn)
  FROM
    stock_info
  WHERE
    rn = 2
)

SELECT
  mr.*,
  smr.close_price as previous_close_price,
  ROUND(((mr.close_price - smr.close_price) / smr.close_price) * 100, 2) as percent_close_price_difference
FROM
  most_recent as mr
  inner join second_most_recent as smr
  on mr.stock_pk = smr.stock_pk
ORDER BY
  ROUND(((mr.close_price - smr.close_price) / smr.close_price) * 100, 2) DESC
LIMIT
  1