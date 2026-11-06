-- Finanzas Local · esquema v1 (se aplica sobre PRAGMA user_version = 0).
-- Requiere SQLite 3.24+ (UPSERT) con JSON1.
--
-- Convenciones
--   * Dinero: INTEGER en centavos. REAL acumula error de redondeo.
--   * Fechas: TEXT 'YYYY-MM-DDTHH:MM:SS' en hora local de la compra, para que un
--     gasto no cambie de día ni de mes al abrir la base desde otra zona horaria.
--   * Las llaves foráneas se activan por conexión: PRAGMA foreign_keys = ON.

-- ---------------------------------------------------------------------------
-- Identidad del entorno. Cada archivo .db guarda su propio entorno_id.
-- ---------------------------------------------------------------------------
CREATE TABLE meta (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
) WITHOUT ROWID;

-- Hash bcrypt del PIN o contraseña. Una sola fila por entorno.
CREATE TABLE credencial (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    tipo           TEXT NOT NULL CHECK (tipo IN ('pin', 'password')),
    hash           BLOB NOT NULL CHECK (typeof(hash) = 'blob'),
    actualizado_en TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Catálogo de lugares. "Éxito", "exito" y " EXITO " comparten la misma clave.
-- ---------------------------------------------------------------------------
CREATE TABLE lugar (
    id     INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL CHECK (length(trim(nombre)) > 0),
    clave  TEXT NOT NULL UNIQUE CHECK (length(clave) > 0)
);

-- ---------------------------------------------------------------------------
-- Gastos
-- ---------------------------------------------------------------------------
CREATE TABLE gasto (
    id             INTEGER PRIMARY KEY,
    lugar_id       INTEGER NOT NULL
                   REFERENCES lugar (id) ON UPDATE RESTRICT ON DELETE RESTRICT,
    total_cent     INTEGER NOT NULL
                   CHECK (typeof(total_cent) = 'integer' AND total_cent > 0),
    -- strftime normaliza fechas imposibles (30 de febrero), por eso esta igualdad
    -- también las rechaza. Se usa IS porque un CHECK que da NULL se considera
    -- aprobado, y strftime devuelve NULL con un formato que no reconoce.
    ocurrido_en    TEXT NOT NULL
                   CHECK (strftime('%Y-%m-%dT%H:%M:%S', ocurrido_en) IS ocurrido_en),
    nota           TEXT,
    creado_en      TEXT NOT NULL,
    actualizado_en TEXT NOT NULL
);

-- El mes se consulta como rango de fechas sobre este índice. Cubre la barra de
-- presupuesto y el Scatter Plot sin leer la tabla, y ordena el historial.
-- No se usan columnas generadas para mes y día: SQLite trata cualquier referencia
-- a ellas como lectura de toda la fila, y el índice dejaría de ser cubriente.
CREATE INDEX ix_gasto_fecha_total ON gasto (ocurrido_en, total_cent);
-- Soporta la llave foránea y el filtro por lugar.
CREATE INDEX ix_gasto_lugar_fecha   ON gasto (lugar_id, ocurrido_en DESC);

CREATE TABLE gasto_item (
    id                  INTEGER PRIMARY KEY,
    gasto_id            INTEGER NOT NULL
                        REFERENCES gasto (id) ON UPDATE RESTRICT ON DELETE CASCADE,
    nombre              TEXT NOT NULL CHECK (length(trim(nombre)) > 0),
    cantidad            INTEGER NOT NULL DEFAULT 1
                        CHECK (typeof(cantidad) = 'integer' AND cantidad > 0),
    valor_unitario_cent INTEGER NOT NULL
                        CHECK (typeof(valor_unitario_cent) = 'integer'
                               AND valor_unitario_cent >= 0)
);

CREATE INDEX ix_gasto_item_gasto ON gasto_item (gasto_id);

-- ---------------------------------------------------------------------------
-- Presupuesto. Un valor rige desde ese mes hasta que aparezca uno más reciente.
-- ---------------------------------------------------------------------------
CREATE TABLE presupuesto (
    vigente_desde TEXT PRIMARY KEY
                  CHECK (vigente_desde GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
                         AND substr(vigente_desde, 6, 2) BETWEEN '01' AND '12'),
    monto_cent    INTEGER NOT NULL
                  CHECK (typeof(monto_cent) = 'integer' AND monto_cent > 0)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- Auditoría: copia del registro antes de cada edición o borrado.
-- gasto_id no es llave foránea para que el rastro sobreviva al borrado.
-- ---------------------------------------------------------------------------
CREATE TABLE auditoria (
    id           INTEGER PRIMARY KEY,
    gasto_id     INTEGER NOT NULL,
    accion       TEXT NOT NULL CHECK (accion IN ('UPDATE', 'DELETE')),
    antes        TEXT NOT NULL CHECK (json_valid(antes)),
    registrado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX ix_auditoria_gasto ON auditoria (gasto_id, id DESC);

CREATE VIEW v_gasto_auditable AS
SELECT g.id AS gasto_id,
       json_object(
           'lugar', l.nombre,
           'total_cent', g.total_cent,
           'ocurrido_en', g.ocurrido_en,
           'nota', g.nota,
           'items', (SELECT json_group_array(json_object(
                                'nombre', i.nombre,
                                'cantidad', i.cantidad,
                                'valor_unitario_cent', i.valor_unitario_cent))
                       FROM gasto_item AS i
                      WHERE i.gasto_id = g.id)
       ) AS antes
  FROM gasto AS g
  JOIN lugar AS l ON l.id = g.lugar_id;

-- BEFORE garantiza que los ítems todavía existen cuando se toma la copia.
CREATE TRIGGER tr_gasto_auditar_update
BEFORE UPDATE ON gasto
BEGIN
    INSERT INTO auditoria (gasto_id, accion, antes)
    SELECT gasto_id, 'UPDATE', antes FROM v_gasto_auditable WHERE gasto_id = OLD.id;
END;

CREATE TRIGGER tr_gasto_auditar_delete
BEFORE DELETE ON gasto
BEGIN
    INSERT INTO auditoria (gasto_id, accion, antes)
    SELECT gasto_id, 'DELETE', antes FROM v_gasto_auditable WHERE gasto_id = OLD.id;
END;
