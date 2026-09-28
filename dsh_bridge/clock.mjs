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
}
