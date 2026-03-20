import { workspace, ExtensionContext, window } from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
  Executable,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;

export async function activate(context: ExtensionContext): Promise<void> {
  const config = workspace.getConfiguration("javaFunctionalLsp");

  if (!config.get<boolean>("enabled", true)) {
    return;
  }

  const serverPath = config.get<string>("serverPath", "java-functional-lsp");

  const serverExecutable: Executable = {
    command: serverPath,
    options: { env: process.env },
  };

  const serverOptions: ServerOptions = {
    run: serverExecutable,
    debug: serverExecutable,
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [
      { scheme: "file", language: "java" },
      { scheme: "untitled", language: "java" },
    ],
    synchronize: {
      fileEvents: workspace.createFileSystemWatcher("**/*.java"),
    },
    outputChannel: window.createOutputChannel("Java Functional LSP"),
  };

  client = new LanguageClient(
    "javaFunctionalLsp",
    "Java Functional LSP",
    serverOptions,
    clientOptions
  );

  try {
    await client.start();
  } catch (error) {
    const msg =
      error instanceof Error ? error.message : "Failed to start server";
    window.showErrorMessage(
      `Java Functional LSP: ${msg}. Is java-functional-lsp installed? (pip install java-functional-lsp)`
    );
  }
}

export async function deactivate(): Promise<void> {
  if (client) {
    await client.stop();
    client = undefined;
  }
}
