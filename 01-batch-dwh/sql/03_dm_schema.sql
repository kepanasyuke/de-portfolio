-- DM layer: Kimball star schema on top of the DDS.
-- dim_customer is SCD2, dim_product is SCD1, fact_sales grain = one order line.

CREATE TABLE IF NOT EXISTS dm.dim_date (
    date_key  integer PRIMARY KEY,
    date      date NOT NULL UNIQUE,
    year      smallint NOT NULL,
    quarter   smallint NOT NULL,
    month     smallint NOT NULL,
    day       smallint NOT NULL,
    iso_week  smallint NOT NULL,
    weekday   smallint NOT NULL,
    is_weekend boolean NOT NULL
);

INSERT INTO dm.dim_date (date_key, date, year, quarter, month, day, iso_week, weekday, is_weekend)
SELECT to_char(d, 'YYYYMMDD')::integer,
       d::date,
       extract(year FROM d)::smallint,
       extract(quarter FROM d)::smallint,
       extract(month FROM d)::smallint,
       extract(day FROM d)::smallint,
       extract(week FROM d)::smallint,
       extract(isodow FROM d)::smallint,
       extract(isodow FROM d) IN (6, 7)
FROM generate_series(date '2023-01-01', date '2027-12-31', interval '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS dm.dim_customer (
    customer_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  text NOT NULL,
    email        text,
    full_name    text,
    country_code text,
    segment      text,
    version      integer NOT NULL,
    valid_from   timestamptz NOT NULL,
    valid_to     timestamptz,
    is_current   boolean NOT NULL DEFAULT true,
    _loaded_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS dim_customer_current_uidx
    ON dm.dim_customer (customer_id) WHERE is_current;

CREATE TABLE IF NOT EXISTS dm.dim_product (
    product_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  text NOT NULL UNIQUE,
    sku         text NOT NULL,
    name        text,
    category    text,
    unit_price  numeric(18, 2) NOT NULL,
    is_active   boolean NOT NULL DEFAULT true,
    _loaded_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dm.fact_sales (
    sales_key       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_item_id   text NOT NULL UNIQUE,
    order_id        text NOT NULL,
    date_key        integer NOT NULL REFERENCES dm.dim_date (date_key),
    customer_key    bigint NOT NULL REFERENCES dm.dim_customer (customer_key),
    product_key     bigint NOT NULL REFERENCES dm.dim_product (product_key),
    quantity        integer NOT NULL,
    gross_amount    numeric(18, 2) NOT NULL,
    discount_amount numeric(18, 2) NOT NULL,
    net_amount      numeric(18, 2) NOT NULL,
    order_status    text NOT NULL,
    _loaded_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fact_sales_date_idx ON dm.fact_sales (date_key);
CREATE INDEX IF NOT EXISTS fact_sales_customer_idx ON dm.fact_sales (customer_key);
CREATE INDEX IF NOT EXISTS fact_sales_product_idx ON dm.fact_sales (product_key);
CREATE INDEX IF NOT EXISTS fact_sales_order_idx ON dm.fact_sales (order_id);
