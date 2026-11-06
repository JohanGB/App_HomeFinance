-- v1 → v2: categoría opcional por gasto e índice para los análisis.

CREATE TABLE categoria (
    id     INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL CHECK (length(trim(nombre)) > 0),
    clave  TEXT NOT NULL UNIQUE CHECK (length(clave) > 0)
);

-- SQLite admite agregar una llave foránea con ALTER TABLE si su valor por defecto es NULL.
ALTER TABLE gasto ADD COLUMN categoria_id INTEGER
    REFERENCES categoria (id) ON UPDATE RESTRICT ON DELETE RESTRICT;

-- Reemplaza a ix_gasto_fecha_total: además del total cubre lugar y categoría, así los
-- totales por día, mes, lugar o categoría de un periodo se responden sin leer la tabla.
DROP INDEX ix_gasto_fecha_total;
CREATE INDEX ix_gasto_analitica ON gasto (ocurrido_en, total_cent, lugar_id, categoria_id);
CREATE INDEX ix_gasto_categoria_fecha ON gasto (categoria_id, ocurrido_en DESC);

-- La auditoría también guarda la categoría anterior.
DROP VIEW v_gasto_auditable;
CREATE VIEW v_gasto_auditable AS
SELECT g.id AS gasto_id,
       json_object(
           'lugar', l.nombre,
           'categoria', c.nombre,
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
  JOIN lugar AS l ON l.id = g.lugar_id
  LEFT JOIN categoria AS c ON c.id = g.categoria_id;
