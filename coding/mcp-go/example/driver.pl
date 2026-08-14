% driver.pl - query driver for the Prolog MCP server.
%
% Invoked as:
%   swipl -q --stack-limit=<bytes> driver.pl \
%         <mode> <programFile|-> <goalFile|-> <limit> <timeoutMs> <sandbox> <depth>
%
% Design notes (these are the reasons this file looks the way it does):
%
%   * All inputs arrive as files or plain argv atoms. Nothing is interpolated
%     into a shell command line, so user-supplied Prolog text can never escape
%     into the shell.
%   * Exactly ONE line of JSON is written to stdout. Prolog diagnostics are
%     trapped by message_hook/3 and returned inside that JSON, so stdout is
%     never corrupted by warnings.
%   * Output written by the user's goal is captured separately and returned in
%     the "output" field.
%   * Requires a full SWI-Prolog installation (the official swipl Docker image).
%     Minimal distro packages such as Debian's swi-prolog-core omit
%     library(time) and library(http/json), which this driver depends on.

:- use_module(library(lists)).
:- use_module(library(apply)).
:- use_module(library(time)).                % call_with_time_limit/2
:- use_module(library(solution_sequences)).  % findnsols/4
:- use_module(library(http/json)).           % json_write_dict/3
:- use_module(library(readutil)).            % read_file_to_string/3
% safe_goal/1. Goals are checked as user:Goal, because resolving them against
% the sandbox module instead would report every user predicate as non-existent.
:- use_module(library(sandbox)).

:- dynamic diag/2.

:- multifile user:message_hook/3.

% Capture errors and warnings instead of letting them reach stderr. Succeeding
% here suppresses the default handler, which is what we want: everything is
% reported back through the JSON result.
user:message_hook(_Term, Kind, Lines) :-
    memberchk(Kind, [error, warning]),
    with_output_to(string(S), print_message_lines(current_output, '', Lines)),
    assertz(diag(Kind, S)).

:- initialization(main, main).

main(Argv) :-
    set_stream(user_output, encoding(utf8)),
    (   Argv = [Mode, ProgF, GoalF, LimitA, TimeoutA, SandboxA, DepthA]
    ->  parse_args(ProgF, GoalF, LimitA, TimeoutA, SandboxA, DepthA, Args),
        (   catch(do(Mode, Args, Result0), E, error_result(E, Result0))
        ->  Result = Result0
        ;   Result = _{ok:false, error:"driver: dispatch failed"}
        ),
        emit(Result)
    ;   emit(_{ok:false, error:"driver: bad arguments"})
    ).

parse_args(ProgF, GoalF, LimitA, TimeoutA, SandboxA, DepthA, Args) :-
    (ProgF == '-' -> Prog = none ; Prog = ProgF),
    (GoalF == '-' -> Goal = "" ; read_file_to_string(GoalF, Goal, [encoding(utf8)])),
    atom_number(LimitA, Limit),
    atom_number(TimeoutA, MS), Secs is max(0.05, MS / 1000),
    (SandboxA == 'true' -> Sandbox = true ; Sandbox = false),
    atom_number(DepthA, Depth),
    Args = args{program:Prog, goal:Goal, limit:Limit, secs:Secs,
                sandbox:Sandbox, depth:Depth}.

% ---------------------------------------------------------------------------
% Modes
% ---------------------------------------------------------------------------

% check: load the program only, and report the predicates it defines.
do(check, Args, Result) :-
    !,
    load_program(Args.program, Loaded),
    program_predicates(Args.program, Preds),
    Result = _{ok:Loaded, predicates:Preds}.

% query: enumerate up to Limit solutions with their variable bindings.
do(query, Args, Result) :-
    !,
    load_program(Args.program, _),
    parse_goal(Args.goal, Goal, Named),
    guard(Args.sandbox, Goal),
    Limit = Args.limit,
    ProbeLimit is Limit + 1,
    with_output_to(string(Out),
        run_query(Goal, Named, ProbeLimit, Args.secs, Found, Status)),
    length(Found, FoundN),
    (   FoundN > Limit
    ->  length(Sols, Limit), append(Sols, [_], Found), Trunc = true
    ;   Sols = Found, Trunc = false
    ),
    length(Sols, N),
    (N > 0 -> Succ = true ; Succ = false),
    (   Status == ok
    ->  Result = _{ok:true, succeeded:Succ, count:N, truncated:Trunc,
                   solutions:Sols, output:Out}
    ;   Status = error(Msg),
        Result = _{ok:false, error:Msg, succeeded:false, count:0,
                   truncated:false, solutions:[], output:Out}
    ).

% prove: semi-deterministic yes/no plus the first binding.
do(prove, Args, Result) :-
    !,
    load_program(Args.program, _),
    parse_goal(Args.goal, Goal, Named),
    guard(Args.sandbox, Goal),
    with_output_to(string(Out),
        run_query(Goal, Named, 1, Args.secs, Sols, Status)),
    (   Status == ok
    ->  ( Sols = [B|_] -> Proved = true, Binding = B
        ; Proved = false, Binding = bindings{} ),
        Result = _{ok:true, proved:Proved, bindings:Binding, output:Out}
    ;   Status = error(Msg),
        Result = _{ok:false, error:Msg, proved:false,
                   bindings:bindings{}, output:Out}
    ).

% explain: build a proof tree for the first solution using a meta-interpreter.
do(explain, Args, Result) :-
    !,
    load_program(Args.program, _),
    parse_goal(Args.goal, Goal, _),
    guard(Args.sandbox, Goal),
    with_output_to(string(Out),
        catch(call_with_time_limit(Args.secs,
                  % First use the native engine to preserve the semantics of
                  % cut, if-then-else and other control constructs that a
                  % small proof-tree interpreter cannot reproduce exactly.
                  ( once(call(Goal)), prove_tree(Goal, Args.depth, Tree)
                  -> Status = proved
                  ; Status = failed )),
              E, (message_text(E, Msg), Status = error))),
    (   Status == proved
    ->  with_output_to(string(Text), render(Tree, 0)),
        Result = _{ok:true, proved:true, proof:Text, output:Out}
    ;   Status == failed
    ->  Result = _{ok:true, proved:false, proof:"", output:Out}
    ;   Result = _{ok:false, error:Msg, proved:false, proof:"", output:Out}
    ).

% match: report which clauses of a unit have a head unifying with a pattern.
do(match, Args, Result) :-
    !,
    match_clauses(Args.program, Args.goal, Result).

do(M, _, _{ok:false, error:E}) :-
    format(string(E), 'unknown mode: ~w', [M]).

% ---------------------------------------------------------------------------
% Clause matching
%
% Reads the clauses of a single unit file as terms, in order, and reports the
% 1-based positions whose head unifies with the pattern. The caller (the Go
% side) splits the same text into the same clause list, so the indices line up;
% "total" lets the caller assert that they really do.
% ---------------------------------------------------------------------------

match_clauses(none, _, _{ok:false, error:"no unit source"}) :- !.
match_clauses(File, PatternS, Result) :-
    parse_pattern(PatternS, Pattern),
    setup_call_cleanup(
        open(File, read, In, [encoding(utf8)]),
        read_matches(In, Pattern, 1, Total, Matches),
        close(In)),
    Result = _{ok:true, total:Total, matches:Matches}.

% A pattern is either a term to unify against the clause head, or Name/Arity.
parse_pattern(PatternS, Pattern) :-
    catch(term_string(P, PatternS), E, throw(E)),
    (   nonvar(P), P = Name/Arity, atom(Name), integer(Arity)
    ->  functor(Pattern, Name, Arity)
    ;   Pattern = P
    ).

read_matches(In, Pattern, I, Total, Matches) :-
    read_term(In, T, []),
    (   T == end_of_file
    ->  Total is I - 1, Matches = []
    ;   clause_head(T, Head),
        (   \+ \+ Head = Pattern
        ->  format(string(HS), '~q', [T]),
            Matches = [_{index:I, text:HS}|Rest]
        ;   Matches = Rest
        ),
        I1 is I + 1,
        read_matches(In, Pattern, I1, Total, Rest)
    ).

clause_head(V, _) :- var(V), !, fail.
clause_head((H :- _), H) :- !.
clause_head((:- _), '$directive') :- !.
clause_head((H --> _), H) :- !.
clause_head(H, H).

% ---------------------------------------------------------------------------
% Program loading
% ---------------------------------------------------------------------------

load_program(none, true) :- !.
load_program(File, Loaded) :-
    (   catch(load_files([File], [silent(true)]), E, (assert_error(E), fail))
    ->  Loaded = true
    ;   Loaded = false
    ).

assert_error(E) :-
    message_text(E, T),
    assertz(diag(error, T)).

% Only report predicates that came from the generated program file, not the
% driver's own predicates or autoloaded library code.
program_predicates(none, []) :- !.
program_predicates(File, Preds) :-
    absolute_file_name(File, Abs),
    findall(_{name:NS, arity:A, dynamic:D},
            ( current_predicate(N/A),
              functor(H, N, A),
              predicate_property(H, file(Abs)),
              atom_string(N, NS),
              ( predicate_property(H, dynamic) -> D = true ; D = false )
            ),
            Preds0),
    sort(Preds0, Preds).

% ---------------------------------------------------------------------------
% Goal handling
% ---------------------------------------------------------------------------

parse_goal(GoalS, Goal, Named) :-
    (   catch(term_string(Goal0, GoalS, [variable_names(Vars)]), E, throw(E))
    ->  true
    ;   throw("goal could not be parsed")
    ),
    ( var(Goal0) -> throw("goal is a bare variable") ; true ),
    Goal = Goal0,
    named_vars(Vars, Named).

% Variables whose name starts with '_' are "don't care" and are not reported.
named_vars([], []).
named_vars([Name=Var|T], Out) :-
    (   sub_atom(Name, 0, 1, _, '_')
    ->  Out = Out1
    ;   Out = [Name-Var|Out1]
    ),
    named_vars(T, Out1).

% Optional sandbox check using library(sandbox), the same mechanism SWISH uses
% to run untrusted Prolog from the public web.
%
% safe_goal/1 rejects any goal that can reach a predicate which does not exist.
% That is the right answer for the goal the caller typed -- it catches typos --
% but it is the wrong answer for a predicate merely *referenced* by a rule in a
% loaded unit. Units are brought in and out of scope deliberately, so a rule
% body that refers to a predicate defined in a unit which is not currently
% loaded should simply fail, in keeping with the closed-world assumption, not
% raise an error.
%
% safe_goal/1 distinguishes the two cases for us: its error carries the chain of
% goals the culprit was reached through. An empty chain means the caller named
% the predicate directly, so it is a genuine error; a non-empty chain means a
% rule reached it, so we declare it dynamic (leaving it with no clauses, hence
% false) and re-check.
guard(false, _) :- !.
guard(true, Goal) :- guard_loop(Goal, 64).

guard_loop(Goal, Budget) :-
    catch(sandbox:safe_goal(user:Goal), E, true),
    (   var(E)
    ->  true
    ;   Budget > 0,
        missing_via_rule(E, Name/Arity)
    ->  assume_undefined(Name, Arity),
        Budget1 is Budget - 1,
        guard_loop(Goal, Budget1)
    ;   message_text(E, T),
        format(string(M), 'sandbox rejected this goal: ~w', [T]),
        throw(M)
    ).

% Succeeds when the sandbox complained about a predicate that a rule body
% reached, rather than one the caller named directly.
missing_via_rule(error(existence_error(procedure, Culprit), sandbox(_, Chain)),
                 Name/Arity) :-
    Chain \== [],
    predicate_id(Culprit, Name, Arity).

predicate_id(_:T, Name, Arity) :- !, predicate_id(T, Name, Arity).
predicate_id(Name/Arity, Name, Arity) :- !, atom(Name), integer(Arity).
predicate_id(T, Name, Arity) :- callable(T), functor(T, Name, Arity).

assume_undefined(Name, Arity) :-
    dynamic(user:Name/Arity),
    format(string(M), 'assumed ~w/~w has no clauses: it is referenced by a rule \c
but is not defined in any unit currently in scope', [Name, Arity]),
    assertz(diag(info, M)).

run_query(Goal, Named, Limit, Secs, Sols, Status) :-
    catch(
        ( call_with_time_limit(Secs,
              ( findnsols(Limit, B, (call(Goal), binding(Named, B)), Sols0) -> true
              ; Sols0 = [] )),
          Sols = Sols0, Status = ok ),
        E,
        ( Sols = [], message_text(E, Msg), Status = error(Msg) )).

binding(Named, Dict) :-
    maplist(one_binding, Named, Pairs),
    dict_pairs(Dict, bindings, Pairs).

one_binding(Name-Var, Name-Str) :-
    format(string(Str), '~q', [Var]).

% ---------------------------------------------------------------------------
% Meta-interpreter for proof trees
% ---------------------------------------------------------------------------

% At the display depth limit, still call the goal before marking its proof as
% elided. Treating the cutoff itself as success would report false goals as
% proved merely because their rule body was deeper than the requested tree.
prove_tree(G, D, node(G, [depth_limit])) :- D =< 0, !, call(G).
prove_tree(true, _, leaf) :- !.
prove_tree((A, B), D, and(TA, TB)) :- !, prove_tree(A, D, TA), prove_tree(B, D, TB).
prove_tree((A ; B), D, T) :- !, ( prove_tree(A, D, T) ; prove_tree(B, D, T) ).
prove_tree(\+ A, D, node(\+A, [])) :- !, \+ prove_tree(A, D, _).
prove_tree(G, _, node(G, [builtin])) :- predicate_property(G, built_in), !, call(G).
prove_tree(G, _, node(G, [foreign])) :- predicate_property(G, foreign), !, call(G).
prove_tree(G, _, node(G, [library(M)])) :-
    predicate_property(G, imported_from(M)),
    !,
    call(M:G).
prove_tree(G, D, node(G, [T])) :-
    D1 is D - 1,
    clause(G, Body),
    prove_tree(Body, D1, T).

render(and(A, B), I) :- !, render(A, I), render(B, I).
render(leaf, _) :- !.
render(node(G, Kids), I) :-
    tab(I), format('~q', [G]),
    render_kids(Kids, I).

render_kids([builtin], _) :- !, format('   % builtin~n').
render_kids([foreign], _) :- !, format('   % foreign~n').
render_kids([library(M)], _) :- !, format('   % library(~q)~n', [M]).
render_kids([depth_limit], _) :- !, format('   % depth limit reached~n').
render_kids([], _)        :- !, nl.
render_kids(Kids, I) :-
    nl,
    I1 is I + 2,
    render_list(Kids, I1).

% Deliberately not maplist([K]>>render(K, I1), Kids): yall copies the lambda
% term, so the free variable I1 would be renamed apart on every call unless it
% were declared as I1/[K]>>render(K, I1).
render_list([], _).
render_list([K|T], I) :- render(K, I), render_list(T, I).

% ---------------------------------------------------------------------------
% Result emission
% ---------------------------------------------------------------------------

emit(Result0) :-
    collect_diags(Ds),
    put_dict(diagnostics, Result0, Ds, Result),
    json_write_dict(current_output, Result, [width(0)]),
    nl,
    flush_output.

collect_diags(Ds) :-
    findall(_{kind:K, message:M}, retract(diag(K, M)), Ds).

error_result(E, _{ok:false, error:T}) :- message_text(E, T).

message_text(E, T) :-
    (   string(E) -> T = E
    ;   atom(E)   -> atom_string(E, T)
    ;   catch(with_output_to(string(T0), print_message(error, E)), _, fail),
        T0 \== ""
    ->  T = T0
    ;   term_string(E, T)
    ).
