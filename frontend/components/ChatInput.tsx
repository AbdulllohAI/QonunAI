import { useState } from "react";

export function ChatInput({ onSend }: any) {
  const [input, setInput] = useState("");

  return (
    <div className="p-4 border-t border-gray-800 flex gap-2">
      <input
        className="flex-1 p-3 bg-black border border-gray-700 rounded-lg"
        value={input}
        placeholder="Ask anything..."
        onChange={(e) => setInput(e.target.value)}
      />
      <button
        onClick={() => {
          if (!input) return;
          onSend(input);
          setInput("");
        }}
        className="px-5 bg-blue-600 rounded-lg"
      >
        Send
      </button>
    </div>
  );
}
