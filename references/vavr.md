# Vavr Functional Programming Reference

Patterns for functional programming with Vavr.

## Railway Pattern with Either

Use `Either<Error, Success>` for error handling. Left track = failure, Right track = success.

```java
// GOOD: Railway-oriented programming
public Either<ValidationError, ProcessedData> process(RawData raw) {
    return validate(raw)
        .flatMap(this::enrich)
        .flatMap(this::transform)
        .peek(data -> metrics.recordSuccess())
        .peekLeft(err -> log.error("Failed", err));
}

// BAD: Exception-based error handling
public ProcessedData process(RawData raw) throws ValidationException {
    // Exceptions break functional composition
}
```

## Option for Null Safety

Never use manual null checks. Use `Option` and functional transformations.

```java
// GOOD: Functional null handling
public Option<UserProfile> findProfile(String userId) {
    return Option.of(cache.get(userId))
        .orElse(() -> Option.of(database.get(userId)))
        .map(this::enrichProfile)
        .filter(UserProfile::isActive);
}

// BAD: Imperative null checks
public UserProfile findProfile(String userId) {
    UserProfile cached = cache.get(userId);
    if (cached == null) {
        cached = database.get(userId);
    }
    if (cached == null) return null;  // AVOID
    return cached.isActive() ? enrichProfile(cached) : null;
}
```

## Option.orElse(Supplier) — Lazy Fallback Pattern

`Option.orElse(Supplier<Option<T>>)` evaluates the supplier **only when the option is empty**.

```java
// GOOD: Lazy fallback — retry only called when cache is empty
return cachedValue.get()
    .orElse(() -> {
        refreshCache();
        return cachedValue.get();
    });
```

## Option Fields: Never Pass Null

```java
@Value
public class UserProfile {
    String name;
    @Nonnull Option<String> nickname;  // @Nonnull documents the contract
}

// Correct:
new UserProfile("John", Option.some("Johnny"));
new UserProfile("John", Option.none());  // Explicit absence

// BAD: Passing null defeats null-safety
new UserProfile("John", null);  // COMPILES but violates contract!
```

## Try for Exception Conversion

```java
public Either<ParseError, JsonNode> parseJson(String raw) {
    return Try.of(() -> objectMapper.readTree(raw))
        .toEither()
        .mapLeft(t -> new ParseError(t.getMessage()));
}
```

## Validation for Accumulating Errors

```java
public Validation<Seq<String>, ValidUser> validateUser(UserInput input) {
    return Validation.combine(
        validateEmail(input.getEmail()),
        validateAge(input.getAge()),
        validateName(input.getName())
    ).ap(ValidUser::new);
}
```

## Side Effects: Managing Purity

**Allowed in peek/map/flatMap**: Logging, metrics
**Forbidden**: Database writes, cache writes, external API calls that modify state

```java
// GOOD: Separate writes as distinct stages
return validate(data)
    .flatMap(this::transform)
    .flatMap(this::persistToDatabase);  // Write is a separate stage

// BAD: Writes mixed with transformations
return validate(data)
    .peek(d -> database.save(d))  // NEVER
    .flatMap(this::transform);
```

## Option.when vs Option.of — Critical Null Gotcha

`Option.when(condition, supplier)` does **not** protect against null. If the supplier returns null, the result is `Some(null)`, NOT `None`.

```java
// BAD: Returns Some(null), NOT None!
Option<String> result = Option.when(enabled, () -> cache.get(key));

// GOOD: Chain with flatMap
Option<String> result = Option.when(enabled, () -> cache.get(key))
    .flatMap(Option::of);  // Some(null) → None

// GOOD: Use Option.of directly
Option<String> result = Option.of(cache.get(key));
```

## Anti-Patterns (CRITICAL)

```java
// BAD: Imperative monad unwrapping
if (option.isDefined()) {           // NEVER do this
    process(option.get());
}

// GOOD: Use transformation
option.forEach(this::process);
option.map(this::transform).getOrElse(defaultValue);

// GOOD: Use fold() for branching
option.fold(
    () -> defaultValue,
    value -> transform(value)
);
```
