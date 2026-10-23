export type CommandReceipt = { session: string; id: string; outcome: { landed: 'new-turn' | 'queued' | 'completed'; why?: string } | { landed: 'ran'; result?: string } | { refused: string } }

declare module 'claude-code' {
  interface PluginState {
    '2mw2lt': { busy: boolean; pending: string[]; inform: string[]; read: string[]; pointed: boolean; appended: boolean; warned: boolean; point: number; session: string; connected: boolean; seat: string; bulletin: string; bulletins: string[]; unsettled: { ulid?: string; say?: string; tries: number }[]; surfaceSeq: number; turn: string; commandReceipts: CommandReceipt[] }
  }
}
