export const createPyodideWorker = () => { throw new Error('Read-only code must not start a formatter worker'); };
export const formatPythonCode = () => { throw new Error('Read-only code must not call a formatter API'); };
export const executeCode = () => { throw new Error('Execution is not part of the rendering test'); };
