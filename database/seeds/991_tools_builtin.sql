-- Tool registry seed: code tools as 'builtin' + 2 demo declarative tools (disabled by default). Idempotent.
INSERT INTO agent_tools (name, kind, description, params_schema, config, intents, enabled) VALUES
 ('search_knowledge','builtin','Busca en la base de conocimiento técnica.',
  '{"type":"object","properties":{"query":{"type":"string"},"top_k":{"type":"integer"}},"required":["query"]}',
  '{}', '{diagnostic,guide}', true),
 ('get_ticket_status','builtin','Estado de un ticket.',
  '{"type":"object","properties":{"ticket_number":{"type":"string","description":"TRM-000123"}},"required":["ticket_number"]}',
  '{}', '{ticket}', true),
 ('create_ticket','builtin','Crea un ticket. Solo si el usuario lo confirmó.',
  '{"type":"object","properties":{"title":{"type":"string"},"description":{"type":"string"},"category":{"type":"string"},"priority":{"type":"string"},"evidence":{"type":"array"}},"required":["title","description","category","priority"]}',
  '{}', '{ticket,diagnostic,output}', true),
 ('get_network_inventory','builtin','Lista dispositivos y redes registrados.',
  '{"type":"object","properties":{"site":{"type":"string"}}}',
  '{}', '{inventory}', true),
 ('get_network_details','builtin','Detalle de una red registrada (gateway, DNS, VLAN).',
  '{"type":"object","properties":{"network_name_or_cidr":{"type":"string"}},"required":["network_name_or_cidr"]}',
  '{}', '{inventory}', true),
 ('parse_command_output','builtin','Analiza una salida de comando pegada por el usuario.',
  '{"type":"object","properties":{"os":{"type":"string"},"command":{"type":"string"},"raw_output":{"type":"string"}},"required":["os","command","raw_output"]}',
  '{}', '{output}', true),
 ('horario_soporte','static_template','Horario de atención del soporte técnico para un día de la semana.',
  '{"type":"object","properties":{"dia":{"type":"string","description":"Día de la semana, por ejemplo lunes"}},"required":["dia"]}',
  '{"template":"El soporte atiende el {{ dia }} de 8:00 a 17:00 (lunes a viernes) y de 8:00 a 12:00 los sábados."}',
  '{guide,smalltalk}', false),
 ('tecnicos_disponibles_zona','sql_readonly','Técnicos disponibles en una zona (nombre, especialidades y nivel).',
  '{"type":"object","properties":{"zona":{"type":"string","description":"Zona, por ejemplo mocoa"}},"required":["zona"]}',
  '{"query":"select name, specialties, level, availability from v_tools_technicians_available where %(zona)s = any(zones) order by name limit 20"}',
  '{ticket,diagnostic}', false)
ON CONFLICT (name) DO NOTHING;

INSERT INTO agent_tool_versions (tool_id, version, snapshot)
SELECT id, version, to_jsonb(agent_tools) FROM agent_tools
ON CONFLICT DO NOTHING;
