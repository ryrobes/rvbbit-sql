IF DB_ID(N'operations') IS NULL
    CREATE DATABASE operations;
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'mirror_reader')
    CREATE LOGIN mirror_reader WITH PASSWORD = 'Rvbbit_Mirror_Readonly_2026!', CHECK_POLICY = ON;
GO

USE operations;
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'mirror_reader')
    CREATE USER mirror_reader FOR LOGIN mirror_reader;
GO

IF SCHEMA_ID(N'field_ops') IS NULL
    EXEC(N'CREATE SCHEMA field_ops');
GO

IF OBJECT_ID(N'field_ops.work_orders', N'U') IS NULL
BEGIN
    CREATE TABLE field_ops.work_orders (
        work_order_id bigint IDENTITY(1,1) PRIMARY KEY,
        work_order_number nvarchar(40) NOT NULL UNIQUE,
        customer_name nvarchar(200) NOT NULL,
        assigned_team nvarchar(100) NULL,
        status nvarchar(40) NOT NULL,
        estimated_hours decimal(10,2) NULL,
        actual_hours decimal(10,2) NULL,
        scheduled_for datetimeoffset NULL,
        completed_at datetimeoffset NULL,
        updated_at datetimeoffset NOT NULL
    );
END;
GO

IF OBJECT_ID(N'field_ops.parts_usage', N'U') IS NULL
BEGIN
    CREATE TABLE field_ops.parts_usage (
        usage_id bigint IDENTITY(1,1) PRIMARY KEY,
        work_order_id bigint NOT NULL REFERENCES field_ops.work_orders(work_order_id),
        sku nvarchar(80) NOT NULL,
        quantity decimal(12,3) NOT NULL,
        unit_cost money NULL,
        notes nvarchar(max) NULL,
        updated_at datetimeoffset NOT NULL
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM field_ops.work_orders)
BEGIN
    INSERT INTO field_ops.work_orders
        (work_order_number, customer_name, assigned_team, status, estimated_hours, actual_hours, scheduled_for, completed_at, updated_at)
    VALUES
        (N'WO-9001', N'Acme & Sons', N'East-1', N'complete', 4.50, 6.25,
         '2026-08-01T08:00:00-04:00', '2026-08-01T15:45:00-04:00', '2026-08-01T15:45:00-04:00'),
        (N'WO-9002', N'Northwind Field Ops', NULL, N'dispatched', 2.00, NULL,
         '2026-08-12T13:30:00-05:00', NULL, '2026-08-11T09:10:00-05:00'),
        (N'WO-9003', N'Muller Industrial', N'Night Shift', N'cancelled', NULL, 0.00,
         NULL, NULL, '2026-08-10T23:59:59+02:00');
END;
GO

IF NOT EXISTS (SELECT 1 FROM field_ops.parts_usage)
BEGIN
    INSERT INTO field_ops.parts_usage
        (work_order_id, sku, quantity, unit_cost, notes, updated_at)
    VALUES
        (1, N'FILTER-20X20', 2.000, 18.75, N'customer supplied one spare', '2026-08-01T15:45:00-04:00'),
        (1, N'REFRIG-R410A', 1.375, 42.10, NULL, '2026-08-01T15:45:00-04:00'),
        (2, N'UNKNOWN', -1.000, NULL, N'legacy correction awaiting review', '2026-08-11T09:10:00-05:00');
END;
GO

GRANT SELECT ON SCHEMA::field_ops TO mirror_reader;
GO
