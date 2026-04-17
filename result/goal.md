Saturn instruction test
需要测量下面的指令或者操作需要多少cycle，或者说分为哪几个阶段，每个阶段要花多少cycle。

1. Memory
   Saturn manual 的 4.5 里面有提到不同的memory access模式 (indexed, segmented)，看看它们需要多少个cycle，那些cycle都花在哪里。load 和 store 或许需要单独考虑。
2. Special instructions
   vector slide, compress, register gather/scatter, reduction
3. Mask generator
   生成mask的指令是如何工作的？它的输出能否被chaining？为什么？