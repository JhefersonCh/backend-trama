-- STT vocabulary hint (Whisper initial prompt) for the active STT profile. Idempotent: only when absent.
UPDATE model_profiles
SET params = params || jsonb_build_object('prompt',
  'Conversación de soporte técnico de internet en Colombia: módem, router, wifi, luz roja, fibra óptica, proveedor, técnico, reinicié, desconecté, ticket, dirección IP, ipconfig.')
WHERE role = 'stt' AND is_active AND NOT (params ? 'prompt');
