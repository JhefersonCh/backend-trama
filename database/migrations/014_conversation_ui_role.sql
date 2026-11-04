-- 014: conversation_messages.role accepts 'ui' (generative UI components persisted as JSON, contract §1). Idempotent.
ALTER TABLE conversation_messages DROP CONSTRAINT IF EXISTS conversation_messages_role_check;
ALTER TABLE conversation_messages ADD CONSTRAINT conversation_messages_role_check
  CHECK (role IN ('system','user','assistant','tool','ui'));
