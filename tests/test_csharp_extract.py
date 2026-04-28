"""Tests for C# AST extraction."""

from __future__ import annotations

from pathlib import Path

from kblens.ast_extract import extract_csharp_file


def _extract(code: str) -> str:
    """Helper: parse code string and return skeleton."""
    return extract_csharp_file(Path("test.cs"), code.encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. Using directives
# ---------------------------------------------------------------------------


def test_using_directives():
    code = """\
using System;
using System.Collections.Generic;
using MyProject.Models;

public class Foo {}
"""
    result = _extract(code)
    assert "using System;" in result
    assert "using System.Collections.Generic;" in result
    assert "using MyProject.Models;" in result


# ---------------------------------------------------------------------------
# 2. Namespace handling
# ---------------------------------------------------------------------------


def test_namespace():
    code = """\
namespace MyApp.Services
{
    public class Foo {}
}
"""
    result = _extract(code)
    assert "namespace MyApp.Services {" in result
    assert "public class Foo {" in result
    assert "}" in result


def test_file_scoped_namespace():
    code = """\
namespace MyApp.Services;

public class Foo {}
"""
    result = _extract(code)
    assert "namespace MyApp.Services;" in result
    assert "public class Foo {" in result


# ---------------------------------------------------------------------------
# 3. Class extraction
# ---------------------------------------------------------------------------


def test_class_with_inheritance():
    code = """\
public class User : BaseEntity, IDisposable
{
    public string Name { get; set; }
}
"""
    result = _extract(code)
    assert "public class User : BaseEntity, IDisposable {" in result
    assert "Name" in result
    assert "get;" in result
    assert "set;" in result


def test_class_filters_private_members():
    code = """\
public class Foo
{
    public int PublicField;
    private int _privateField;
    protected string ProtectedProp { get; set; }
    internal void InternalMethod() { }
    private void PrivateMethod() { }
}
"""
    result = _extract(code)
    assert "PublicField" in result
    assert "_privateField" not in result
    assert "ProtectedProp" in result
    assert "InternalMethod" in result
    assert "PrivateMethod" not in result


def test_method_body_stripped():
    code = """\
public class Svc
{
    public async Task<User> CreateAsync(CreateUserRequest request)
    {
        var user = new User();
        await db.SaveAsync(user);
        return user;
    }
}
"""
    result = _extract(code)
    assert "CreateAsync" in result
    assert "CreateUserRequest request" in result
    # Body should not appear
    assert "SaveAsync" not in result
    assert "new User()" not in result


def test_expression_bodied_method():
    code = """\
public class Calc
{
    public int Double(int x) => x * 2;
}
"""
    result = _extract(code)
    assert "Double" in result
    assert "int x" in result
    # Expression body should not appear
    assert "x * 2" not in result


# ---------------------------------------------------------------------------
# 4. Struct
# ---------------------------------------------------------------------------


def test_struct():
    code = """\
public struct Vector3
{
    public float X, Y, Z;
    public float Length() => MathF.Sqrt(X*X + Y*Y + Z*Z);
}
"""
    result = _extract(code)
    assert "public struct Vector3 {" in result
    assert "float X" in result
    assert "Length" in result
    assert "MathF.Sqrt" not in result


# ---------------------------------------------------------------------------
# 5. Interface
# ---------------------------------------------------------------------------


def test_interface():
    code = """\
public interface IRepository<T> where T : class
{
    Task<T?> GetByIdAsync(int id);
    Task<IEnumerable<T>> GetAllAsync();
    string Name { get; }
}
"""
    result = _extract(code)
    assert "public interface IRepository<T>" in result
    assert "where T : class" in result
    assert "GetByIdAsync" in result
    assert "GetAllAsync" in result
    assert "Name" in result


# ---------------------------------------------------------------------------
# 6. Enum
# ---------------------------------------------------------------------------


def test_enum():
    code = """\
public enum Status
{
    Active,
    Inactive,
    Deleted
}
"""
    result = _extract(code)
    assert "public enum Status" in result
    assert "Active" in result
    assert "Inactive" in result
    assert "Deleted" in result


# ---------------------------------------------------------------------------
# 7. Record types
# ---------------------------------------------------------------------------


def test_positional_record():
    code = """\
public record Person(string FirstName, string LastName);
"""
    result = _extract(code)
    assert "public record Person" in result
    assert "string FirstName" in result
    assert "string LastName" in result


def test_record_with_body():
    code = """\
public record Person(string Name)
{
    public int Age { get; set; }
}
"""
    result = _extract(code)
    assert "public record Person" in result
    assert "string Name" in result
    assert "Age" in result


def test_record_struct():
    code = """\
public record struct Point(double X, double Y);
"""
    result = _extract(code)
    assert "record struct Point" in result
    assert "double X" in result


# ---------------------------------------------------------------------------
# 8. Delegate
# ---------------------------------------------------------------------------


def test_delegate():
    code = """\
public delegate void EventCallback(string message, int code);
"""
    result = _extract(code)
    assert "delegate" in result
    assert "EventCallback" in result
    assert "string message" in result


# ---------------------------------------------------------------------------
# 9. Attributes
# ---------------------------------------------------------------------------


def test_attributes_preserved():
    code = """\
[Serializable]
[Obsolete("Use NewClass instead")]
public class OldClass
{
    [Required]
    public string Name { get; set; }
}
"""
    result = _extract(code)
    assert "[Serializable]" in result
    assert "[Obsolete" in result
    assert "public class OldClass" in result


# ---------------------------------------------------------------------------
# 10. XML doc comments
# ---------------------------------------------------------------------------


def test_xml_doc_comments():
    code = """\
/// <summary>Manages user accounts.</summary>
public class UserService
{
    /// <summary>Creates a new user.</summary>
    /// <param name="request">The creation request.</param>
    public async Task<User> CreateAsync(CreateUserRequest request)
    {
        return null;
    }
}
"""
    result = _extract(code)
    assert "/// <summary>Manages user accounts.</summary>" in result
    assert "/// <summary>Creates a new user.</summary>" in result


# ---------------------------------------------------------------------------
# 11. Nested types
# ---------------------------------------------------------------------------


def test_nested_class():
    code = """\
public class Outer
{
    public class Inner
    {
        public void Do() {}
    }

    private class Secret {}
}
"""
    result = _extract(code)
    assert "public class Outer {" in result
    assert "public class Inner {" in result
    assert "Do" in result
    assert "Secret" not in result


# ---------------------------------------------------------------------------
# 12. Properties with various patterns
# ---------------------------------------------------------------------------


def test_auto_property():
    code = """\
public class Foo
{
    public string Name { get; set; }
    public int Id { get; init; }
    public string ReadOnly { get; }
}
"""
    result = _extract(code)
    assert "get;" in result
    assert "set;" in result
    assert "init;" in result


def test_expression_bodied_property():
    code = """\
public class Foo
{
    public string FullName => FirstName + LastName;
}
"""
    result = _extract(code)
    assert "FullName" in result
    # Expression body should be simplified
    assert "FirstName + LastName" not in result


# ---------------------------------------------------------------------------
# 13. Constructor
# ---------------------------------------------------------------------------


def test_constructor():
    code = """\
public class User
{
    public User(string name, string email)
    {
        Name = name;
        Email = email;
    }

    public string Name { get; }
    public string Email { get; }
}
"""
    result = _extract(code)
    assert "User(string name, string email)" in result
    assert "Name = name" not in result


# ---------------------------------------------------------------------------
# 14. Events and delegates in class
# ---------------------------------------------------------------------------


def test_events():
    code = """\
public class Button
{
    public event EventHandler? Clicked;
    public event EventHandler<string> TextChanged;
}
"""
    result = _extract(code)
    assert "event EventHandler? Clicked" in result
    assert "event EventHandler<string> TextChanged" in result


# ---------------------------------------------------------------------------
# 15. Generic class with constraints
# ---------------------------------------------------------------------------


def test_generic_class_with_constraints():
    code = """\
public class Repository<T, TKey> : IRepository<T>
    where T : class, IEntity
    where TKey : struct
{
    public Task<T?> FindAsync(TKey id) { return null; }
}
"""
    result = _extract(code)
    assert "Repository<T, TKey>" in result
    assert "IRepository<T>" in result
    assert "where T : class, IEntity" in result
    assert "where TKey : struct" in result
    assert "FindAsync" in result


# ---------------------------------------------------------------------------
# 16. Static class with extension methods
# ---------------------------------------------------------------------------


def test_static_class():
    code = """\
public static class StringExtensions
{
    public static string ToUpperFirst(this string s)
    {
        return char.ToUpper(s[0]) + s[1..];
    }
}
"""
    result = _extract(code)
    assert "public static class StringExtensions" in result
    assert "ToUpperFirst" in result
    assert "this string s" in result
    assert "char.ToUpper" not in result


# ---------------------------------------------------------------------------
# 17. Comprehensive integration test
# ---------------------------------------------------------------------------


def test_comprehensive():
    code = """\
using System;
using System.Collections.Generic;

namespace MyApp.Models
{
    /// <summary>Base entity.</summary>
    public abstract class BaseEntity
    {
        public int Id { get; set; }
        public DateTime CreatedAt { get; init; }
    }

    public interface IRepository<T> where T : class
    {
        Task<T?> GetByIdAsync(int id);
    }

    public enum Status { Active, Inactive }

    public record Person(string Name, int Age);

    public sealed class User : BaseEntity, IRepository<User>
    {
        private readonly string _secret;

        public string Name { get; set; }

        public User(string name) { _secret = name; }

        public async Task<User?> GetByIdAsync(int id)
        {
            return await Task.FromResult<User?>(null);
        }

        protected virtual void OnChanged() { }

        private void Log(string msg) { }
    }
}
"""
    result = _extract(code)

    # Using directives
    assert "using System;" in result
    assert "using System.Collections.Generic;" in result

    # Namespace
    assert "namespace MyApp.Models {" in result

    # BaseEntity
    assert "/// <summary>Base entity.</summary>" in result
    assert "public abstract class BaseEntity {" in result
    assert "Id" in result
    assert "CreatedAt" in result

    # Interface
    assert "public interface IRepository<T>" in result
    assert "GetByIdAsync" in result

    # Enum
    assert "public enum Status" in result

    # Record
    assert "public record Person" in result

    # User class
    assert "public sealed class User : BaseEntity, IRepository<User> {" in result
    assert "_secret" not in result  # private field filtered out
    assert "public string Name" in result
    assert "User(string name)" in result
    assert "GetByIdAsync" in result
    assert "OnChanged" in result  # protected → visible
    assert "Log" not in result  # private → filtered

    # No method bodies
    assert "Task.FromResult" not in result


# ---------------------------------------------------------------------------
# 18. Compound access modifiers
# ---------------------------------------------------------------------------


def test_protected_internal():
    code = """\
public class Foo
{
    protected internal void SharedMethod() { }
    private protected void DerivedOnlyMethod() { }
    private void HiddenMethod() { }
}
"""
    result = _extract(code)
    assert "SharedMethod" in result       # protected internal → visible
    assert "DerivedOnlyMethod" in result  # private protected → visible (has protected)
    assert "HiddenMethod" not in result   # private → hidden


# ---------------------------------------------------------------------------
# 19. Attributes not duplicated in header (regression test for P0 bug)
# ---------------------------------------------------------------------------


def test_attributes_not_duplicated_in_header():
    code = """\
[DisplayName("My Tool")]
[Category("Graphics")]
public class MyTool : BaseTool
{
    public void Run() { }
}
"""
    result = _extract(code)
    # Attributes should appear exactly once, NOT be embedded in the class header line
    lines = result.split("\n")
    attr_lines = [l for l in lines if "[DisplayName" in l]
    assert len(attr_lines) == 1, f"Expected 1 attr line, got {len(attr_lines)}: {attr_lines}"
    class_line = [l for l in lines if "public class MyTool" in l][0]
    assert "[DisplayName" not in class_line, f"Attr leaked into header: {class_line}"


# ---------------------------------------------------------------------------
# 20. Top-level type without access modifier (default internal → visible)
# ---------------------------------------------------------------------------


def test_default_internal_class():
    code = """\
namespace Foo
{
    class InternalByDefault
    {
        public void DoStuff() {}
    }
}
"""
    result = _extract(code)
    assert "class InternalByDefault" in result
    assert "DoStuff" in result
