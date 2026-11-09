// Decompile every function in the imported program and print C-like pseudocode.
//
// Why this exists: `analyzeHeadless` only imports and analyses — without a
// post-script it prints nothing the model can read. The GUI decompiler is not
// available in a headless container, so this script is the bridge between
// "ghidra is installed" and "the model can actually read the decompilation".
//
// Written in Java on purpose: Ghidra's Python script provider (PyGhidra) is not
// guaranteed to be present in the headless package; Java scripts always work.
//
// Usage (the recipe passes it):
//   analyzeHeadless <proj_dir> <proj_name> -import <binary> \
//       -scriptPath /opt/ghidra-scripts -postScript DecompileAll.java -deleteProject
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.util.task.ConsoleTaskMonitor;

public class DecompileAll extends GhidraScript {
    @Override
    public void run() throws Exception {
        DecompInterface ifc = new DecompInterface();
        ifc.openProgram(currentProgram);
        ConsoleTaskMonitor monitor = new ConsoleTaskMonitor();
        int count = 0;
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        while (it.hasNext()) {
            Function f = it.next();
            DecompileResults res = ifc.decompileFunction(f, 60, monitor);
            if (res != null && res.decompileCompleted()) {
                println("// ==== " + f.getName() + " @ " + f.getEntryPoint());
                println(res.getDecompiledFunction().getC());
                count++;
            }
        }
        println("// decompiled " + count + " functions");
    }
}
