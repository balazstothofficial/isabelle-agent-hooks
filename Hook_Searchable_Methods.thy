theory Hook_Searchable_Methods
  imports Main
begin

ML \<open>
local
  val bools = [false, true]
  val ctxt = \<^context>
  val try0_methods = Try0.get_all_proof_method_names ()
  val sledgehammer_methods =
    maps (fn smt_proofs =>
      maps (fn needs_full_types =>
        maps (fn needs_lam_defs =>
          Sledgehammer_Prover.bunches_of_proof_methods
            ctxt smt_proofs needs_full_types needs_lam_defs
          |> flat
          |> map (Sledgehammer_Proof_Methods.string_of_proof_method []))
        bools)
      bools)
    bools
  val methods = sort_distinct string_ord (try0_methods @ sledgehammer_methods)
in
  val _ = List.app (writeln o prefix "NO_GUESSED_PROOFS_METHOD ") methods
end
\<close>

end
