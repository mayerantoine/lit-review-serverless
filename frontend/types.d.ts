// Type declarations for external libraries

declare module '@microsoft/fetch-event-source' {
  export interface EventSourceMessage {
    id?: string;
    event?: string;
    data: string;
    retry?: number;
  }

  export interface FetchEventSourceInit extends RequestInit {
    onopen?: (response: Response) => void | Promise<void>;
    onmessage?: (event: EventSourceMessage) => void;
    onerror?: (err: unknown) => void;
    onclose?: () => void;
    openWhenHidden?: boolean;
  }

  export function fetchEventSource(
    input: RequestInfo,
    init: FetchEventSourceInit
  ): Promise<void>;
}
