export const name = 'phoebe-clock';
export const inject = ['tools'];

export function apply(ctx) {
  // Register the public ToolDefinition directly: standalone SDK wheels do not
  // expose their embedded packages to bare imports from an external module.
  ctx.tools.register({
    name: 'phoebe_time',
    description: 'Get the actual current date and time in Asia/Shanghai (UTC+8). Read only.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute() {
      return new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai' }) + ' UTC+8';
    },
  });
  const schemas = JSON.parse(process.env.PHOEBE_TOOL_SCHEMAS || '[]');
  const channel = JSON.parse(process.env.PHOEBE_TOOL_CHANNEL || '{}');
  if (schemas.length && (!channel.url || !channel.token)) {
    throw new Error('Missing request-scoped tool channel');
  }
  for (const schema of schemas) {
    ctx.tools.register({
      name: schema.name,
      description: schema.description,
      parameters: schema.parameters,
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      async execute(args) {
        const response = await fetch(channel.url, {
          method: 'POST',
          headers: { Authorization: `Bearer ${channel.token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: schema.name, arguments: args }),
          redirect: 'error',
          signal: AbortSignal.timeout(21000),
        });
        if (!response.ok) return '查询失败，未得到可核实结果。';
        const result = await response.json();
        return (result.isError ? '[工具失败] ' : '') + result.text;
      },
    });
  }
}
