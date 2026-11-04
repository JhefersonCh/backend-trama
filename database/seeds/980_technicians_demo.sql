-- Demo technicians (synthetic). Fixed UUIDs, idempotent.
INSERT INTO technicians (id, name, email_or_username, specialty, specialties, zones, level, max_open_tickets, availability, schedule, contact_channel, data_origin) VALUES
 ('a0000000-0000-4000-8000-000000000001', 'Laura Mejía', 'laura.mejia', 'fibra', '{fibra,redes}', '{mocoa,villagarzon}', 'n2', 5, 'disponible', '{"lun-vie":["07:00","18:00"],"sab":["08:00","12:00"]}', 'laura.mejia', 'synthetic'),
 ('a0000000-0000-4000-8000-000000000002', 'Carlos Rosero', 'carlos.rosero', 'wifi', '{wifi,cableado}', '{puerto_asis,orito,valle_del_guamuez}', 'n2', 4, 'disponible', '{"lun-vie":["08:00","17:00"]}', 'carlos.rosero', 'synthetic'),
 ('a0000000-0000-4000-8000-000000000003', 'Diana Chindoy', 'diana.chindoy', 'redes', '{redes,wifi,equipos}', '{sibundoy,mocoa}', 'n1', 6, 'disponible', '{"lun-vie":["08:00","17:00"]}', 'diana.chindoy', 'synthetic'),
 ('a0000000-0000-4000-8000-000000000004', 'Andrés Quintero', 'andres.quintero', 'cableado', '{cableado,fibra,cobre_dsl}', '{}', 'n2', 3, 'disponible', '{"lun-vie":["06:00","20:00"]}', 'andres.quintero', 'synthetic'),
 ('a0000000-0000-4000-8000-000000000005', 'Marcela Tisoy', 'marcela.tisoy', 'redes', '{redes,fibra}', '{puerto_asis,mocoa}', 'n1', 5, 'vacaciones', '{"lun-vie":["08:00","17:00"]}', 'marcela.tisoy', 'synthetic')
ON CONFLICT DO NOTHING;
